"""
scraper.py — полный парсер mixer-cup.gg
Собирает данные по ВСЕМ турнирам, накапливает историю игроков
"""

import os, re, json, time, urllib.request
from datetime import datetime, timezone
from collections import defaultdict
import requests

API_URL        = "https://api.mixer-cup.gg/"
OPENDOTA_URL   = "https://api.opendota.com/api"
TOURNAMENT_IDS = [26, 27]  # все турниры
TEAM_PAGE_BASE = "https://mixer-cup.gg/ru/team/"
CACHE_FILE     = "matches_cache.json"

HEADERS = {
    "Content-Type": "application/json",
    "Accept": "*/*",
    "Origin": "https://mixer-cup.gg",
    "Referer": "https://mixer-cup.gg/",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36",
}

Q_TEAMS = """
query Teams($filters: TeamFilterInput!, $first: Int, $offset: Int, $sort: [TeamSortEnum]) {
  teams(first: $first, offset: $offset, filters: $filters, sort: $sort) {
    pageInfo { total totalFiltered }
    items {
      id name number place
      stats { totalWin gamesLost gamesWon gamesTotal }
      players { id nickname rating proName }
    }
  }
}
"""

Q_TEAM = """
query Team($id: UUID!) {
  team(id: $id) {
    id name number place
    players { id nickname rating leaderboardRank steamAvatar proName }
  }
}
"""

Q_GAMES = """
query Games($first: Int, $offset: Int, $filters: GameFilterInput) {
  games(first: $first, offset: $offset, filters: $filters) {
    pageInfo { total totalFiltered }
    items {
      id status matchId result startTime endTime
      team1 { id name number }
      team2 { id name number }
    }
  }
}
"""

# ─── helpers ──────────────────────────────────────────────────────────────────

def gql(operation, query, variables=None):
    payload = {"operationName": operation, "query": query, "variables": variables or {}}
    r = requests.post(API_URL, headers=HEADERS, json=payload, timeout=15)
    r.raise_for_status()
    data = r.json()
    if "errors" in data:
        raise ValueError(data["errors"])
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

# ─── Gist ─────────────────────────────────────────────────────────────────────

def gist_get(gist_id, gist_token):
    req = urllib.request.Request(
        f"https://api.github.com/gists/{gist_id}",
        headers={
            "Authorization": f"token {gist_token}",
            "Accept": "application/vnd.github.v3+json",
            "User-Agent": "mixer-cup-scraper",
        }
    )
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read())

def upload_to_gist(filename, content_str):
    gist_id    = os.environ.get("GIST_ID", "").strip()
    gist_token = os.environ.get("GIST_TOKEN", "").strip()
    if not gist_id or not gist_token:
        print(f"  (нет GIST_ID/GIST_TOKEN — пропускаю {filename})")
        return
    payload = json.dumps({"files": {filename: {"content": content_str}}}).encode()
    req = urllib.request.Request(
        f"https://api.github.com/gists/{gist_id}",
        data=payload, method="PATCH",
        headers={
            "Authorization": f"token {gist_token}",
            "Accept": "application/vnd.github.v3+json",
            "Content-Type": "application/json",
            "User-Agent": "mixer-cup-scraper",
        }
    )
    try:
        with urllib.request.urlopen(req) as resp:
            print(f"  ✓ {filename} → Gist (HTTP {resp.status})")
    except Exception as e:
        print(f"  [!] Gist error {filename}: {e}")

def load_from_gist(filename):
    gist_id    = os.environ.get("GIST_ID", "").strip()
    gist_token = os.environ.get("GIST_TOKEN", "").strip()
    if not gist_id or not gist_token:
        return None
    try:
        gist_data = gist_get(gist_id, gist_token)
        if filename in gist_data.get("files", {}):
            raw_url = gist_data["files"][filename]["raw_url"]
            with urllib.request.urlopen(raw_url) as resp:
                return json.loads(resp.read())
    except Exception as e:
        print(f"  [!] Не удалось загрузить {filename} из Gist: {e}")
    return None

# ─── Heroes ───────────────────────────────────────────────────────────────────

def fetch_heroes():
    print("[→] Загружаю героев из OpenDota...")
    r = requests.get(f"{OPENDOTA_URL}/heroes", timeout=10)
    r.raise_for_status()
    heroes = {}
    for h in r.json():
        heroes[h["id"]] = {"name": h["localized_name"], "slug": h["name"].replace("npc_dota_hero_", "")}
    print(f"  Героев: {len(heroes)}")
    return heroes

# ─── Teams ────────────────────────────────────────────────────────────────────

def fetch_all_teams(tournament_id):
    all_items, first, offset = [], 30, 0
    while True:
        data = gql("Teams", Q_TEAMS, {
            "filters": {"tournamentId": tournament_id},
            "sort": "NUMBER", "first": first, "offset": offset,
        })
        page  = data.get("teams", {})
        items = page.get("items", [])
        total = page.get("pageInfo", {}).get("totalFiltered", 0)
        all_items.extend(items)
        print(f"  Команд: {len(all_items)} / {total}", end="\r")
        if len(all_items) >= total or not items:
            break
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

# ─── Games ────────────────────────────────────────────────────────────────────

def fetch_all_games(tournament_id):
    all_items, first, offset = [], 350, 0
    while True:
        data = gql("Games", Q_GAMES, {
            "filters": {"tournamentId": tournament_id},
            "first": first, "offset": offset,
        })
        page  = data.get("games", {})
        items = page.get("items", [])
        total = page.get("pageInfo", {}).get("totalFiltered", 0)
        all_items.extend(items)
        if len(all_items) >= total or not items:
            break
        offset += first
        time.sleep(0.2)
    return all_items

def fetch_opendota_match(match_id, cache):
    mid = str(match_id)
    if mid in cache:
        return cache[mid], True
    try:
        r = requests.get(f"{OPENDOTA_URL}/matches/{mid}", timeout=15)
        if r.status_code == 404:
            return None, False
        r.raise_for_status()
        data = r.json()
        cache[mid] = data
        return data, False
    except Exception as e:
        print(f"    [!] OpenDota {mid}: {e}")
        return None, False

# ─── Match processing ─────────────────────────────────────────────────────────

def process_match(game, match_data, heroes, steam64_to_player):
    if not match_data:
        return None

    radiant_win = match_data.get("radiant_win")
    duration    = match_data.get("duration", 0)
    match_id    = str(game["matchId"])
    result      = game.get("result", "")
    team1_id    = game["team1"]["id"]
    team2_id    = game["team2"]["id"]
    winner_id   = team1_id if result == "WIN1" else (team2_id if result == "WIN2" else None)

    players_out = []
    for p in (match_data.get("players") or []):
        acc_id = p.get("account_id")
        if not acc_id:
            continue
        sid64    = steam32_to_64(acc_id)
        hero_id  = p.get("hero_id", 0)
        hero     = heroes.get(hero_id, {"name": f"Hero#{hero_id}", "slug": ""})
        slot     = p.get("player_slot", 0)
        is_rad   = slot < 128
        win      = (is_rad and radiant_win) or (not is_rad and not radiant_win)
        pinfo    = steam64_to_player.get(sid64, {})
        players_out.append({
            "steam_id":  sid64,
            "nickname":  pinfo.get("nickname", f"Steam:{sid64}"),
            "team_id":   pinfo.get("team_id", ""),
            "hero_id":   hero_id,
            "hero_name": hero["name"],
            "hero_slug": hero["slug"],
            "is_radiant":is_rad,
            "win":       win,
            "kills":     p.get("kills", 0),
            "deaths":    p.get("deaths", 0),
            "assists":   p.get("assists", 0),
            "gpm":       p.get("gold_per_min", 0),
            "xpm":       p.get("xp_per_min", 0),
        })

    picks_bans = match_data.get("picks_bans") or []
    picks, bans = [], []
    for pb in picks_bans:
        hero_id  = pb.get("hero_id", 0)
        hero     = heroes.get(hero_id, {"name": f"Hero#{hero_id}", "slug": ""})
        entry = {
            "hero_id":   hero_id,
            "hero_name": hero["name"],
            "hero_slug": hero["slug"],
            "team":      pb.get("team", 0),
            "order":     pb.get("order", 0),
            "is_pick":   pb.get("is_pick", False),
        }
        if pb.get("is_pick"):
            picks.append(entry)
        else:
            bans.append(entry)

    mins = duration // 60
    secs = duration % 60

    return {
        "match_id":     match_id,
        "game_id":      game["id"],
        "tournament_id":game.get("tournamentId", ""),
        "status":       game["status"],
        "result":       result,
        "winner_id":    winner_id,
        "team1":        game["team1"],
        "team2":        game["team2"],
        "start_time":   game.get("startTime"),
        "duration":     f"{mins}:{secs:02d}",
        "players":      players_out,
        "picks":        sorted(picks, key=lambda x: x["order"]),
        "bans":         sorted(bans,  key=lambda x: x["order"]),
        "all_picks_bans": sorted(picks + bans, key=lambda x: x["order"]),
        "opendota_url": f"https://www.opendota.com/matches/{match_id}",
    }

# ─── Player history ───────────────────────────────────────────────────────────

def build_hero_stats(all_matches, sid):
    """Строит статистику героев для одного игрока по его steam_id."""
    stats = defaultdict(lambda: {"picks": 0, "wins": 0, "hero_name": "", "hero_slug": ""})
    for match in all_matches:
        for p in match.get("players", []):
            if p["steam_id"] == sid:
                hid = p["hero_id"]
                stats[hid]["picks"] += 1
                stats[hid]["hero_name"] = p["hero_name"]
                stats[hid]["hero_slug"] = p["hero_slug"]
                if p["win"]:
                    stats[hid]["wins"] += 1
    result = []
    for hid, s in stats.items():
        picks = s["picks"]
        wins  = s["wins"]
        result.append({
            "hero_id":   hid,
            "hero_name": s["hero_name"],
            "hero_slug": s["hero_slug"],
            "picks":     picks,
            "wins":      wins,
            "losses":    picks - wins,
            "winrate":   round(wins / picks * 100, 1) if picks else 0,
        })
    result.sort(key=lambda h: (-h["picks"], -h["winrate"]))
    return result

def merge_player_history(existing_history, steam64_to_player, all_matches, current_tournament_id):
    """
    Накапливает историю игроков по всем турнирам.
    existing_history: dict steam_id -> {...} из предыдущих запусков
    """
    history = existing_history.copy()

    # Собираем все матчи по игроку
    by_player = defaultdict(list)
    for match in all_matches:
        for p in match["players"]:
            sid = p["steam_id"]
            if sid:
                by_player[sid].append(match)

    for sid, matches in by_player.items():
        pinfo = steam64_to_player.get(sid, {})
        hero_stats = build_hero_stats(matches, sid)
        total  = sum(h["picks"] for h in hero_stats)
        wins   = sum(h["wins"]  for h in hero_stats)

        if sid not in history:
            history[sid] = {
                "steam_id":    sid,
                "nickname":    pinfo.get("nickname", f"Steam:{sid}"),
                "steam_url":   f"https://steamcommunity.com/profiles/{sid}",
                "dotabuff_url":f"https://www.dotabuff.com/players/{dotabuff_id(sid)}",
                "tournaments": {},
                "heroes_all":  {},
            }

        # Обновляем ник если есть
        if pinfo.get("nickname"):
            history[sid]["nickname"] = pinfo["nickname"]

        # Статистика по этому турниру
        history[sid]["tournaments"][str(current_tournament_id)] = {
            "total_games": total,
            "total_wins":  wins,
            "overall_wr":  round(wins / total * 100, 1) if total else 0,
            "heroes":      hero_stats,
        }

        # Накопленная статистика по всем героям за все турниры
        heroes_all = defaultdict(lambda: {"picks": 0, "wins": 0, "hero_name": "", "hero_slug": ""})
        for t_data in history[sid]["tournaments"].values():
            for h in t_data["heroes"]:
                hid = str(h["hero_id"])
                heroes_all[hid]["picks"]     += h["picks"]
                heroes_all[hid]["wins"]      += h["wins"]
                heroes_all[hid]["hero_name"]  = h["hero_name"]
                heroes_all[hid]["hero_slug"]  = h["hero_slug"]

        all_heroes_list = []
        for hid, s in heroes_all.items():
            p2 = s["picks"]
            w2 = s["wins"]
            all_heroes_list.append({
                "hero_id":   int(hid),
                "hero_name": s["hero_name"],
                "hero_slug": s["hero_slug"],
                "picks":     p2,
                "wins":      w2,
                "losses":    p2 - w2,
                "winrate":   round(w2 / p2 * 100, 1) if p2 else 0,
            })
        all_heroes_list.sort(key=lambda h: (-h["picks"], -h["winrate"]))
        history[sid]["heroes_all"] = all_heroes_list

        total_all = sum(h["picks"] for h in all_heroes_list)
        wins_all  = sum(h["wins"]  for h in all_heroes_list)
        history[sid]["total_games_all"] = total_all
        history[sid]["total_wins_all"]  = wins_all
        history[sid]["overall_wr_all"]  = round(wins_all / total_all * 100, 1) if total_all else 0

    return history

# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    heroes = fetch_heroes()

    # Загружаем кэш матчей из Gist
    print("\n[→] Загружаю кэш матчей из Gist...")
    cache = load_from_gist("matches_cache.json") or {}
    if cache:
        print(f"  Кэш: {len(cache)} матчей")
    else:
        print("  Кэш пуст — начинаем с нуля")

    # Загружаем историю игроков из Gist
    print("[→] Загружаю историю игроков из Gist...")
    players_history = load_from_gist("players_history.json") or {}
    print(f"  История: {len(players_history)} игроков")

    all_teams_out = []
    new_fetched_total = 0

    for TOURNAMENT_ID in TOURNAMENT_IDS:
        print(f"\n{'='*50}")
        print(f"[ТУРНИР {TOURNAMENT_ID}]")

        # Команды
        print(f"[→] Загружаю команды...")
        teams_raw = fetch_all_teams(TOURNAMENT_ID)
        if not teams_raw:
            print(f"  Нет команд для турнира {TOURNAMENT_ID}, пропускаю")
            continue
        print(f"  Найдено: {len(teams_raw)}")

        steam64_to_player = {}
        teams_out = []

        print("[→] Загружаю Steam данные игроков...")
        for i, raw in enumerate(teams_raw):
            print(f"  [{i+1:>2}/{len(teams_raw)}] {raw['name']}", end="  ", flush=True)
            players_brief = {p["id"]: p for p in (raw.get("players") or [])}
            players_full  = fetch_team_steam(raw["id"])
            time.sleep(0.25)

            players = []
            for pid in (list(players_brief.keys()) or list(players_full.keys())):
                brief  = players_brief.get(pid, {})
                full   = players_full.get(pid, {})
                merged = {**brief, **{k: v for k, v in full.items() if v is not None}}
                sid    = steam_id_from_avatar(merged.get("steamAvatar"))
                db_id  = dotabuff_id(sid) if sid else None
                nick   = merged.get("proName") or merged.get("nickname") or "—"
                player = {
                    "id":          pid,
                    "nickname":    nick,
                    "rating":      merged.get("rating"),
                    "steam_id":    sid,
                    "steam_url":   f"https://steamcommunity.com/profiles/{sid}" if sid else None,
                    "dotabuff_url":f"https://www.dotabuff.com/players/{db_id}" if db_id else None,
                    "leaderboard": merged.get("leaderboardRank"),
                    "heroes":      [],
                    "total_games": 0,
                    "total_wins":  0,
                    "overall_wr":  0,
                }
                players.append(player)
                if sid:
                    steam64_to_player[sid] = {
                        "nickname":  nick,
                        "team_id":   raw["id"],
                        "team_name": raw["name"],
                    }

            stats = raw.get("stats") or {}
            teams_out.append({
                "id":            raw["id"],
                "name":          raw["name"],
                "number":        raw.get("number"),
                "place":         raw.get("place"),
                "tournament_id": TOURNAMENT_ID,
                "games_won":     stats.get("gamesWon"),
                "games_lost":    stats.get("gamesLost"),
                "team_rating":   sum(p["rating"] or 0 for p in players),
                "dom_order":     raw.get("number") or i,
                "url":           TEAM_PAGE_BASE + raw["id"],
                "players":       players,
                "matches":       [],
            })
            print(f"→  {len(players)} игроков")

        # Матчи
        print(f"\n[→] Загружаю игры турнира {TOURNAMENT_ID}...")
        all_games = fetch_all_games(TOURNAMENT_ID)
        completed = [g for g in all_games if g.get("status") == "COMPLETE" and g.get("matchId")]
        print(f"  Всего: {len(all_games)}  |  Завершено: {len(completed)}")

        print(f"[→] Загружаю данные матчей из OpenDota...")
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
            processed = process_match(game, match_data, heroes, steam64_to_player)
            if processed:
                all_matches.append(processed)
                print(f"→  {len(processed['picks'])}pk  {len(processed['bans'])}bn")
            else:
                print("→  нет данных")

        new_fetched_total += new_fetched
        print(f"  Новых матчей: {new_fetched}")

        # Обновляем историю игроков
        players_history = merge_player_history(
            players_history, steam64_to_player, all_matches, TOURNAMENT_ID
        )

        # Привязываем матчи к командам и героев к игрокам
        team_map = {t["id"]: t for t in teams_out}
        for match in all_matches:
            for tid in [match["team1"]["id"], match["team2"]["id"]]:
                if tid in team_map:
                    team_map[tid]["matches"].append(match)

        for team in teams_out:
            for player in team["players"]:
                sid = player["steam_id"]
                if sid and sid in players_history:
                    ph = players_history[sid]
                    t_data = ph["tournaments"].get(str(TOURNAMENT_ID), {})
                    player["heroes"]      = t_data.get("heroes", [])
                    player["total_games"] = t_data.get("total_games", 0)
                    player["total_wins"]  = t_data.get("total_wins", 0)
                    player["overall_wr"]  = t_data.get("overall_wr", 0)
                    # Добавляем данные за все турниры
                    player["heroes_all"]      = ph.get("heroes_all", [])
                    player["total_games_all"] = ph.get("total_games_all", 0)
                    player["overall_wr_all"]  = ph.get("overall_wr_all", 0)

        all_teams_out.extend(teams_out)

    # Сохраняем кэш
    with open(CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False)
    if new_fetched_total > 0:
        print(f"\n[→] Загружаю кэш в Gist ({len(cache)} матчей)...")
        upload_to_gist("matches_cache.json", json.dumps(cache, ensure_ascii=False))

    # Сохраняем историю игроков
    history_str = json.dumps(players_history, ensure_ascii=False, indent=2, default=str)
    with open("players_history.json", "w", encoding="utf-8") as f:
        f.write(history_str)
    print(f"[→] Загружаю историю игроков в Gist ({len(players_history)} игроков)...")
    upload_to_gist("players_history.json", history_str)

    # Финальный вывод
    output = {
        "updated_at":    datetime.now(timezone.utc).isoformat(),
        "tournaments":   TOURNAMENT_IDS,
        "matches_done":  sum(len(t.get("matches", [])) for t in all_teams_out) // 2,
        "teams":         all_teams_out,
    }
    content_str = json.dumps(output, ensure_ascii=False, indent=2, default=str)
    with open("teams_data.json", "w", encoding="utf-8") as f:
        f.write(content_str)
    print("✓ teams_data.json сохранён")
    upload_to_gist("teams_data.json", content_str)

if __name__ == "__main__":
    main()
