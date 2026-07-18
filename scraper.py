"""
Полный парсер mixer-cup.gg с умным кэшированием
Парсит только новые матчи, накапливает историю игроков
"""

import os, re, json, time, urllib.request
from datetime import datetime, timezone
from collections import defaultdict
import requests

API_URL        = "https://api.mixer-cup.gg/"
OPENDOTA_URL   = "https://api.opendota.com/api"
TOURNAMENT_IDS = [26, 27]
TEAM_PAGE_BASE = "https://mixer-cup.gg/ru/team/"
CACHE_FILE     = "matches_cache.json"
HISTORY_FILE   = "players_history.json"

HEADERS = {
    "Content-Type": "application/json",
    "Accept": "*/*",
    "Origin": "https://mixer-cup.gg",
    "Referer": "https://mixer-cup.gg/",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36",
}

Q_TEAMS = """query Teams($filters: TeamFilterInput!, $first: Int, $offset: Int, $sort: [TeamSortEnum]) {
  teams(first: $first, offset: $offset, filters: $filters, sort: $sort) {
    pageInfo { total totalFiltered }
    items { id name number place
      stats { totalWin gamesLost gamesWon gamesTotal }
      players { id nickname rating proName }
    }
  }
}"""

Q_TEAM = """query Team($id: UUID!) {
  team(id: $id) {
    id name number place
    players { id nickname rating leaderboardRank steamAvatar proName }
  }
}"""

Q_GAMES = """query Games($first: Int, $offset: Int, $filters: GameFilterInput) {
  games(first: $first, offset: $offset, filters: $filters) {
    pageInfo { total totalFiltered }
    items { id status matchId result startTime endTime team1 { id name number } team2 { id name number } }
  }
}"""

def gql(op, query, vars=None):
    r = requests.post(API_URL, headers=HEADERS, json={"operationName": op, "query": query, "variables": vars or {}}, timeout=15)
    r.raise_for_status()
    data = r.json()
    if "errors" in data: raise ValueError(data["errors"])
    return data.get("data", {})

def steam_id_from_avatar(url):
    m = re.search(r'/avatars/(\d{15,})', url or "")
    return m.group(1) if m else None

def dotabuff_id(sid):
    try: return str(int(sid) - 76561197960265728)
    except: return None

def steam32_to_64(acc_id):
    try: return str(int(acc_id) + 76561197960265728)
    except: return None

def upload_gist(filename, content_str):
    gist_id = os.environ.get("GIST_ID", "").strip()
    gist_token = os.environ.get("GIST_TOKEN", "").strip()
    if not gist_id or not gist_token: return
    try:
        payload = json.dumps({"files": {filename: {"content": content_str}}}).encode()
        req = urllib.request.Request(
            f"https://api.github.com/gists/{gist_id}",
            data=payload, method="PATCH",
            headers={"Authorization": f"token {gist_token}", "Accept": "application/vnd.github.v3+json", "Content-Type": "application/json", "User-Agent": "mixer-cup-scraper"}
        )
        with urllib.request.urlopen(req) as resp:
            print(f"  ✓ {filename} → Gist")
    except Exception as e:
        print(f"  [!] Gist error {filename}: {e}")

def fetch_heroes():
    print("[→] Герои из OpenDota...")
    r = requests.get(f"{OPENDOTA_URL}/heroes", timeout=10)
    r.raise_for_status()
    return {h["id"]: {"name": h["localized_name"], "slug": h["name"].replace("npc_dota_hero_", "")} for h in r.json()}

def fetch_all_teams(tid):
    all_items, first, offset = [], 30, 0
    while True:
        data = gql("Teams", Q_TEAMS, {"filters": {"tournamentId": tid}, "sort": "NUMBER", "first": first, "offset": offset})
        items = data.get("teams", {}).get("items", [])
        total = data.get("teams", {}).get("pageInfo", {}).get("totalFiltered", 0)
        all_items.extend(items)
        print(f"  Команд: {len(all_items)} / {total}", end="\r")
        if len(all_items) >= total or not items: break
        offset += first
        time.sleep(0.2)
    print()
    return all_items

def fetch_team_steam(team_id):
    try:
        data = gql("Team", Q_TEAM, {"id": team_id})
        return {p["id"]: p for p in (data.get("team") or {}).get("players", [])}
    except Exception as e:
        print(f"    [!] {team_id}: {e}")
        return {}

def fetch_all_games(tid):
    all_items, first, offset = [], 350, 0
    while True:
        data = gql("Games", Q_GAMES, {"filters": {"tournamentId": tid}, "first": first, "offset": offset})
        items = data.get("games", {}).get("items", [])
        total = data.get("games", {}).get("pageInfo", {}).get("totalFiltered", 0)
        all_items.extend(items)
        if len(all_items) >= total or not items: break
        offset += first
        time.sleep(0.2)
    return all_items

def fetch_opendota_match(match_id, cache):
    mid = str(match_id)
    if mid in cache: return cache[mid], True
    try:
        r = requests.get(f"{OPENDOTA_URL}/matches/{mid}", timeout=15)
        if r.status_code == 404: return None, False
        r.raise_for_status()
        data = r.json()
        cache[mid] = data
        return data, False
    except Exception as e:
        print(f"    [!] OpenDota {mid}: {e}")
        return None, False

def process_match(game, match_data, heroes, s2p):
    if not match_data: return None
    radiant_win = match_data.get("radiant_win")
    duration = match_data.get("duration", 0)
    match_id = str(game["matchId"])
    result = game.get("result", "")
    team1_id, team2_id = game["team1"]["id"], game["team2"]["id"]
    winner_id = team1_id if result == "WIN1" else (team2_id if result == "WIN2" else None)
    
    players_out = []
    for p in (match_data.get("players") or []):
        acc_id = p.get("account_id")
        if not acc_id: continue
        sid64 = steam32_to_64(acc_id)
        hero_id = p.get("hero_id", 0)
        hero = heroes.get(hero_id, {"name": f"Hero#{hero_id}", "slug": ""})
        slot = p.get("player_slot", 0)
        is_rad = slot < 128
        win = (is_rad and radiant_win) or (not is_rad and not radiant_win)
        pinfo = s2p.get(sid64, {})
        players_out.append({
            "steam_id": sid64, "nickname": pinfo.get("nickname", f"Steam:{sid64}"),
            "team_id": pinfo.get("team_id", ""), "hero_id": hero_id,
            "hero_name": hero["name"], "hero_slug": hero["slug"],
            "is_radiant": is_rad, "win": win,
            "kills": p.get("kills", 0), "deaths": p.get("deaths", 0), "assists": p.get("assists", 0),
            "gpm": p.get("gold_per_min", 0), "xpm": p.get("xp_per_min", 0),
        })

    picks_bans = match_data.get("picks_bans") or []
    picks, bans = [], []
    for pb in picks_bans:
        hero_id = pb.get("hero_id", 0)
        hero = heroes.get(hero_id, {"name": f"Hero#{hero_id}", "slug": ""})
        entry = {
            "hero_id": hero_id, "hero_name": hero["name"], "hero_slug": hero["slug"],
            "team": pb.get("team", 0), "order": pb.get("order", 0), "is_pick": pb.get("is_pick", False),
        }
        (picks if pb.get("is_pick") else bans).append(entry)

    mins, secs = divmod(duration, 60)
    return {
        "match_id": match_id, "game_id": game["id"], "tournament_id": game.get("tournamentId", ""),
        "status": game["status"], "result": result, "winner_id": winner_id,
        "team1": game["team1"], "team2": game["team2"], "start_time": game.get("startTime"),
        "duration": f"{mins}:{secs:02d}", "players": players_out,
        "picks": sorted(picks, key=lambda x: x["order"]),
        "bans": sorted(bans, key=lambda x: x["order"]),
        "all_picks_bans": sorted(picks + bans, key=lambda x: x["order"]),
        "opendota_url": f"https://www.opendota.com/matches/{match_id}",
    }

def build_hero_stats(matches, sid):
    stats = defaultdict(lambda: {"picks": 0, "wins": 0, "hero_name": "", "hero_slug": ""})
    for match in matches:
        for p in match.get("players", []):
            if p["steam_id"] == sid:
                hid = p["hero_id"]
                stats[hid]["picks"] += 1
                stats[hid]["hero_name"] = p["hero_name"]
                stats[hid]["hero_slug"] = p["hero_slug"]
                if p["win"]: stats[hid]["wins"] += 1
    result = []
    for hid, s in stats.items():
        p2, w2 = s["picks"], s["wins"]
        result.append({
            "hero_id": hid, "hero_name": s["hero_name"], "hero_slug": s["hero_slug"],
            "picks": p2, "wins": w2, "losses": p2 - w2, "winrate": round(w2 / p2 * 100, 1) if p2 else 0,
        })
    result.sort(key=lambda h: (-h["picks"], -h["winrate"]))
    return result

def merge_history(existing, s2p, matches, tournament_id):
    history = existing.copy()
    by_player = defaultdict(list)
    for match in matches:
        for p in match["players"]:
            sid = p["steam_id"]
            if sid: by_player[sid].append(match)

    for sid, matches_list in by_player.items():
        pinfo = s2p.get(sid, {})
        hero_stats = build_hero_stats(matches_list, sid)
        total = sum(h["picks"] for h in hero_stats)
        wins = sum(h["wins"] for h in hero_stats)

        if sid not in history:
            history[sid] = {
                "steam_id": sid, "nickname": pinfo.get("nickname", f"Steam:{sid}"),
                "steam_url": f"https://steamcommunity.com/profiles/{sid}",
                "dotabuff_url": f"https://www.dotabuff.com/players/{dotabuff_id(sid)}",
                "tournaments": {}, "heroes_all": {},
            }

        if pinfo.get("nickname"): history[sid]["nickname"] = pinfo["nickname"]
        history[sid]["tournaments"][str(tournament_id)] = {
            "total_games": total, "total_wins": wins, "overall_wr": round(wins / total * 100, 1) if total else 0,
            "heroes": hero_stats,
        }

        heroes_all = defaultdict(lambda: {"picks": 0, "wins": 0, "hero_name": "", "hero_slug": ""})
        for t_data in history[sid]["tournaments"].values():
            for h in t_data["heroes"]:
                hid = str(h["hero_id"])
                heroes_all[hid]["picks"] += h["picks"]
                heroes_all[hid]["wins"] += h["wins"]
                heroes_all[hid]["hero_name"] = h["hero_name"]
                heroes_all[hid]["hero_slug"] = h["hero_slug"]

        all_h_list = []
        for hid, s in heroes_all.items():
            p2, w2 = s["picks"], s["wins"]
            all_h_list.append({
                "hero_id": int(hid), "hero_name": s["hero_name"], "hero_slug": s["hero_slug"],
                "picks": p2, "wins": w2, "losses": p2 - w2, "winrate": round(w2 / p2 * 100, 1) if p2 else 0,
            })
        all_h_list.sort(key=lambda h: (-h["picks"], -h["winrate"]))
        history[sid]["heroes_all"] = all_h_list

        total_all = sum(h["picks"] for h in all_h_list)
        wins_all = sum(h["wins"] for h in all_h_list)
        history[sid]["total_games_all"] = total_all
        history[sid]["total_wins_all"] = wins_all
        history[sid]["overall_wr_all"] = round(wins_all / total_all * 100, 1) if total_all else 0

    return history

def main():
    print("╔════════════════════════════════╗")
    print("║  Mixer Cup Parser v2            ║")
    print("╚════════════════════════════════╝")

    # Загрузка кэша и истории
    print("\n[→] Загружаю кэш...")
    cache = {}
    if os.path.exists(CACHE_FILE):
        try:
            cache = json.loads(open(CACHE_FILE).read())
            print(f"  Кэш: {len(cache)} матчей")
        except:
            print("  Кэш повреждён, начинаю с нуля")
    else:
        print("  Нет кэша, первый запуск")

    print("[→] Загружаю историю игроков...")
    history = {}
    if os.path.exists(HISTORY_FILE):
        try:
            history = json.loads(open(HISTORY_FILE).read())
            print(f"  История: {len(history)} игроков")
        except:
            print("  История повреждена, начинаю с нуля")
    else:
        print("  Нет истории")

    heroes = fetch_heroes()
    all_teams_out = []

    for TOURNAMENT_ID in TOURNAMENT_IDS:
        print(f"\n{'='*40}\n[ТУРНИР {TOURNAMENT_ID}]")

        print("[→] Команды...")
        teams_raw = fetch_all_teams(TOURNAMENT_ID)
        if not teams_raw: continue
        print(f"  Найдено: {len(teams_raw)}")

        s2p = {}
        teams_out = []

        print("[→] Steam данные...")
        for i, raw in enumerate(teams_raw):
            print(f"  [{i+1:>2}/{len(teams_raw)}] {raw['name']}", end="  ", flush=True)
            pb = {p["id"]: p for p in (raw.get("players") or [])}
            pf = fetch_team_steam(raw["id"])
            time.sleep(0.25)

            players = []
            for pid in (list(pb.keys()) or list(pf.keys())):
                b = pb.get(pid, {})
                f = pf.get(pid, {})
                m = {**b, **{k: v for k, v in f.items() if v is not None}}
                sid = steam_id_from_avatar(m.get("steamAvatar"))
                db_id = dotabuff_id(sid) if sid else None
                nick = m.get("proName") or m.get("nickname") or "—"
                player = {
                    "id": pid, "nickname": nick, "rating": m.get("rating"),
                    "steam_id": sid, "steam_url": f"https://steamcommunity.com/profiles/{sid}" if sid else None,
                    "dotabuff_url": f"https://www.dotabuff.com/players/{db_id}" if db_id else None,
                    "leaderboard": m.get("leaderboardRank"),
                    "heroes": [], "total_games": 0, "total_wins": 0, "overall_wr": 0,
                }
                players.append(player)
                if sid:
                    s2p[sid] = {"nickname": nick, "team_id": raw["id"], "team_name": raw["name"]}

            st = raw.get("stats") or {}
            teams_out.append({
                "id": raw["id"], "name": raw["name"], "number": raw.get("number"),
                "place": raw.get("place"), "tournament_id": TOURNAMENT_ID,
                "games_won": st.get("gamesWon"), "games_lost": st.get("gamesLost"),
                "team_rating": sum(p["rating"] or 0 for p in players),
                "dom_order": raw.get("number") or i, "url": TEAM_PAGE_BASE + raw["id"],
                "players": players, "matches": [],
            })
            print(f"→  {len(players)} игроков")

        print(f"\n[→] Игры турнира {TOURNAMENT_ID}...")
        all_games = fetch_all_games(TOURNAMENT_ID)
        completed = [g for g in all_games if g.get("status") == "COMPLETE" and g.get("matchId")]
        print(f"  Всего: {len(all_games)}  |  Завершено: {len(completed)}")

        print(f"[→] OpenDota ({len(completed)} матчей)...")
        all_matches = []
        new_fetched = 0

        for i, game in enumerate(completed):
            mid = str(game["matchId"])
            match_data, from_cache = fetch_opendota_match(mid, cache)
            tag = "кэш" if from_cache else "API"
            print(f"  [{i+1:>3}/{len(completed)}] {mid} ({tag})", end="  ", flush=True)
            if not from_cache:
                new_fetched += 1
                time.sleep(1.1)
            processed = process_match(game, match_data, heroes, s2p)
            if processed:
                all_matches.append(processed)
                print(f"→  {len(processed['picks'])}pk")
            else:
                print("→  skip")

        print(f"  Новых: {new_fetched}")

        # Обновляем историю
        history = merge_history(history, s2p, all_matches, TOURNAMENT_ID)

        # Привязываем матчи и героев
        team_map = {t["id"]: t for t in teams_out}
        for match in all_matches:
            for tid in [match["team1"]["id"], match["team2"]["id"]]:
                if tid in team_map:
                    team_map[tid]["matches"].append(match)

        for team in teams_out:
            for player in team["players"]:
                sid = player["steam_id"]
                if sid and sid in history:
                    ph = history[sid]
                    t_data = ph["tournaments"].get(str(TOURNAMENT_ID), {})
                    player["heroes"] = t_data.get("heroes", [])
                    player["total_games"] = t_data.get("total_games", 0)
                    player["total_wins"] = t_data.get("total_wins", 0)
                    player["overall_wr"] = t_data.get("overall_wr", 0)
                    player["heroes_all"] = ph.get("heroes_all", [])
                    player["total_games_all"] = ph.get("total_games_all", 0)
                    player["overall_wr_all"] = ph.get("overall_wr_all", 0)

        all_teams_out.extend(teams_out)

    # Сохранение
    print(f"\n[→] Сохраняю локально...")
    with open(CACHE_FILE, "w") as f:
        json.dump(cache, f)
    print(f"  ✓ Кэш: {len(cache)} матчей")

    with open(HISTORY_FILE, "w") as f:
        json.dump(history, f, ensure_ascii=False, indent=2, default=str)
    print(f"  ✓ История: {len(history)} игроков")

    output = {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "tournaments": TOURNAMENT_IDS,
        "matches_done": sum(len(t.get("matches", [])) for t in all_teams_out) // 2,
        "teams": all_teams_out,
    }
    with open("teams_data.json", "w") as f:
        json.dump(output, f, ensure_ascii=False, indent=2, default=str)
    print(f"  ✓ teams_data.json")

    print(f"\n[→] Загружаю в Gist...")
    upload_gist("matches_cache.json", json.dumps(cache, ensure_ascii=False))
    upload_gist("players_history.json", json.dumps(history, ensure_ascii=False, indent=2, default=str))
    upload_gist("teams_data.json", json.dumps(output, ensure_ascii=False, indent=2, default=str))

    print("\n✓ Done!")

if __name__ == "__main__":
    main()
