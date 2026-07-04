"""
scraper.py — полный парсер mixer-cup.gg
Собирает: команды, игроков, матчи, пики/баны, аналитику героев
Сохраняет: teams_data.json
"""

import os, re, json, time, urllib.request
from datetime import datetime, timezone
from collections import defaultdict
import requests

API_URL        = "https://api.mixer-cup.gg/"
OPENDOTA_URL   = "https://api.opendota.com/api"
TOURNAMENT_ID  = 26
TEAM_PAGE_BASE = "https://mixer-cup.gg/ru/team/"
CACHE_FILE     = "matches_cache.json"

HEADERS = {
    "Content-Type": "application/json",
    "Accept": "*/*",
    "Origin": "https://mixer-cup.gg",
    "Referer": "https://mixer-cup.gg/",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36",
}

# ─── GraphQL запросы ──────────────────────────────────────────────────────────

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

# ─── API helpers ──────────────────────────────────────────────────────────────

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

# ─── Загрузка героев ──────────────────────────────────────────────────────────

def fetch_heroes():
    print("[→] Загружаю список героев из OpenDota...")
    r = requests.get(f"{OPENDOTA_URL}/heroes", timeout=10)
    r.raise_for_status()
    heroes = {}
    for h in r.json():
        heroes[h["id"]] = {
            "name": h["localized_name"],
            "slug": h["name"].replace("npc_dota_hero_", ""),
        }
    print(f"  Героев: {len(heroes)}")
    return heroes

# ─── Загрузка команд ──────────────────────────────────────────────────────────

def fetch_all_teams():
    all_items, first, offset = [], 30, 0
    while True:
        data = gql("Teams", Q_TEAMS, {
            "filters": {"tournamentId": TOURNAMENT_ID},
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

# ─── Загрузка матчей ──────────────────────────────────────────────────────────

def fetch_all_games():
    all_items, first, offset = [], 350, 0
    while True:
        data = gql("Games", Q_GAMES, {
            "filters": {"tournamentId": TOURNAMENT_ID},
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

# ─── Обработка матча ──────────────────────────────────────────────────────────

def process_match(game, match_data, heroes, steam64_to_player):
    """Строит структуру матча с пиками, банами, игроками."""
    if not match_data:
        return None

    radiant_win  = match_data.get("radiant_win")
    picks_bans   = match_data.get("picks_bans") or []
    duration     = match_data.get("duration", 0)
    match_id     = str(game["matchId"])

    # Определяем какая команда radiant/dire по steam id игроков
    # team: 0=radiant, 1=dire
    # result: WIN1=team1 победила, WIN2=team2 победила

    team1_id = game["team1"]["id"]
    team2_id = game["team2"]["id"]
    result   = game.get("result", "")

    # WIN1 = team1 победила
    if result == "WIN1":
        winner_id = team1_id
    elif result == "WIN2":
        winner_id = team2_id
    else:
        winner_id = None

    # Игроки матча
    players_out = []
    for p in (match_data.get("players") or []):
        acc_id  = p.get("account_id")
        if not acc_id:
            continue
        sid64   = steam32_to_64(acc_id)
        hero_id = p.get("hero_id", 0)
        hero    = heroes.get(hero_id, {"name": f"Hero#{hero_id}", "slug": ""})
        slot    = p.get("player_slot", 0)
        is_radiant = slot < 128
        is_pick_win = (is_radiant and radiant_win) or (not is_radiant and not radiant_win)

        player_info = steam64_to_player.get(sid64, {})

        players_out.append({
            "steam_id":   sid64,
            "nickname":   player_info.get("nickname", f"Steam:{sid64}"),
            "team_id":    player_info.get("team_id", ""),
            "hero_id":    hero_id,
            "hero_name":  hero["name"],
            "hero_slug":  hero["slug"],
            "is_radiant": is_radiant,
            "win":        is_pick_win,
            "kills":      p.get("kills", 0),
            "deaths":     p.get("deaths", 0),
            "assists":    p.get("assists", 0),
            "gpm":        p.get("gold_per_min", 0),
            "xpm":        p.get("xp_per_min", 0),
        })

    # Пики и баны
    picks = []
    bans  = []
    for pb in picks_bans:
        hero_id  = pb.get("hero_id", 0)
        hero     = heroes.get(hero_id, {"name": f"Hero#{hero_id}", "slug": ""})
        team_num = pb.get("team", 0)  # 0=radiant, 1=dire
        entry = {
            "hero_id":   hero_id,
            "hero_name": hero["name"],
            "hero_slug": hero["slug"],
            "team":      team_num,
            "order":     pb.get("order", 0),
        }
        if pb.get("is_pick"):
            picks.append(entry)
        else:
            bans.append(entry)

    # Длительность
    mins = duration // 60
    secs = duration % 60
    duration_str = f"{mins}:{secs:02d}"

    return {
        "match_id":    match_id,
        "game_id":     game["id"],
        "status":      game["status"],
        "result":      result,
        "winner_id":   winner_id,
        "team1":       game["team1"],
        "team2":       game["team2"],
        "start_time":  game.get("startTime"),
        "duration":    duration_str,
        "players":     players_out,
        "picks":       sorted(picks, key=lambda x: x["order"]),
        "bans":        sorted(bans,  key=lambda x: x["order"]),
        "opendota_url": f"https://www.opendota.com/matches/{match_id}",
    }

# ─── Аналитика героев по игроку ───────────────────────────────────────────────

def build_player_hero_stats(all_matches, steam64_to_player):
    """Для каждого игрока строит статистику по героям."""
    stats = defaultdict(lambda: defaultdict(lambda: {
        "picks": 0, "wins": 0,
        "hero_name": "", "hero_slug": "",
    }))

    for match in all_matches:
        for p in match.get("players", []):
            sid  = p["steam_id"]
            hid  = p["hero_id"]
            s    = stats[sid][hid]
            s["picks"]     += 1
            s["hero_name"]  = p["hero_name"]
            s["hero_slug"]  = p["hero_slug"]
            if p["win"]:
                s["wins"] += 1

    result = {}
    for sid, heroes in stats.items():
        hero_list = []
        for hid, s in heroes.items():
            picks = s["picks"]
            wins  = s["wins"]
            hero_list.append({
                "hero_id":   hid,
                "hero_name": s["hero_name"],
                "hero_slug": s["hero_slug"],
                "picks":     picks,
                "wins":      wins,
                "losses":    picks - wins,
                "winrate":   round(wins / picks * 100, 1) if picks else 0,
            })
        hero_list.sort(key=lambda h: (-h["picks"], -h["winrate"]))
        result[sid] = hero_list
    return result

# ─── Gist upload ──────────────────────────────────────────────────────────────

def upload_to_gist(filename, content_str):
    gist_id    = os.environ.get("GIST_ID", "").strip()
    gist_token = os.environ.get("GIST_TOKEN", "").strip()
    if not gist_id or not gist_token:
        print(f"  (GIST_ID/GIST_TOKEN не заданы — пропускаю {filename})")
        return
    print(f"[→] Загружаю {filename} в Gist...")
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
        print(f"  [!] Gist error: {e}")

# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    # 1. Герои
    heroes = fetch_heroes()

    # 2. Команды
    print(f"\n[→] Загружаю команды турнира ID={TOURNAMENT_ID}...")
    teams_raw = fetch_all_teams()
    print(f"  Найдено: {len(teams_raw)}")

    # Строим карту steam64 -> {nickname, team_id, team_name}
    steam64_to_player = {}
    teams_out = []

    print("\n[→] Загружаю Steam данные игроков...")
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
                "heroes":      [],  # заполним позже
            }
            players.append(player)

            if sid:
                steam64_to_player[sid] = {
                    "nickname":  nick,
                    "team_id":   raw["id"],
                    "team_name": raw["name"],
                    "player_idx": len(players) - 1,
                }

        stats = raw.get("stats") or {}
        teams_out.append({
            "id":          raw["id"],
            "name":        raw["name"],
            "number":      raw.get("number"),
            "place":       raw.get("place"),
            "games_won":   stats.get("gamesWon"),
            "games_lost":  stats.get("gamesLost"),
            "team_rating": sum(p["rating"] or 0 for p in players),
            "dom_order":   raw.get("number") or i,
            "url":         TEAM_PAGE_BASE + raw["id"],
            "players":     players,
            "matches":     [],  # заполним позже
        })
        sid_count = sum(1 for p in players if p["steam_id"])
        print(f"→  {len(players)} игроков  |  {sid_count} Steam ID")

    # 3. Матчи
    print(f"\n[→] Загружаю список игр...")
    all_games  = fetch_all_games()
    completed  = [g for g in all_games if g.get("status") == "COMPLETE" and g.get("matchId")]
    print(f"  Всего игр: {len(all_games)}  |  Завершено: {len(completed)}")

    # Загружаем кэш — сначала из Gist, потом из локального файла
    cache = {}
    gist_id    = os.environ.get("GIST_ID", "").strip()
    gist_token = os.environ.get("GIST_TOKEN", "").strip()
    if gist_id and gist_token:
        try:
            import urllib.request as ur
            req = ur.Request(
                f"https://api.github.com/gists/{gist_id}",
                headers={
                    "Authorization": f"token {gist_token}",
                    "Accept": "application/vnd.github.v3+json",
                    "User-Agent": "mixer-cup-scraper",
                }
            )
            with ur.urlopen(req) as resp:
                gist_data = json.loads(resp.read())
            if "matches_cache.json" in gist_data.get("files", {}):
                raw_url = gist_data["files"]["matches_cache.json"]["raw_url"]
                with ur.urlopen(raw_url) as resp:
                    cache = json.loads(resp.read())
                print(f"  Кэш из Gist: {len(cache)} матчей")
            else:
                print("  Кэш в Gist не найден — начинаем с нуля")
        except Exception as e:
            print(f"  [!] Не удалось загрузить кэш из Gist: {e}")
    if not cache and os.path.exists(CACHE_FILE):
        try:
            cache = json.loads(open(CACHE_FILE, encoding="utf-8").read())
            print(f"  Кэш локальный: {len(cache)} матчей")
        except:
            pass

    print(f"\n[→] Загружаю данные матчей из OpenDota...")
    all_matches = []
    new_fetched = 0

    for i, game in enumerate(completed):
        mid = str(game["matchId"])
        match_data, from_cache = fetch_opendota_match(mid, cache)

        tag = "кэш" if from_cache else "API"
        print(f"  [{i+1:>3}/{len(completed)}] {mid} ({tag})", end="  ", flush=True)

        if not from_cache:
            new_fetched += 1
            time.sleep(1.1)  # rate limit

        processed = process_match(game, match_data, heroes, steam64_to_player)
        if processed:
            all_matches.append(processed)
            print(f"→  {len(processed['picks'])} пиков  |  {len(processed['bans'])} банов")
        else:
            print("→  нет данных")

    # Сохраняем кэш
    with open(CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False)
    print(f"\n  Кэш обновлён: {len(cache)} матчей (новых: {new_fetched})")
    # Сохраняем кэш в Gist чтобы следующий запуск не качал заново
    if new_fetched > 0:
        cache_str = json.dumps(cache, ensure_ascii=False)
        upload_to_gist("matches_cache.json", cache_str)

    # 4. Аналитика героев по игрокам
    print(f"\n[→] Считаю статистику героев...")
    hero_stats = build_player_hero_stats(all_matches, steam64_to_player)

    # Привязываем матчи к командам и героев к игрокам
    team_map = {t["id"]: t for t in teams_out}

    for match in all_matches:
        t1_id = match["team1"]["id"]
        t2_id = match["team2"]["id"]
        if t1_id in team_map:
            team_map[t1_id]["matches"].append(match)
        if t2_id in team_map:
            team_map[t2_id]["matches"].append(match)

    for team in teams_out:
        for player in team["players"]:
            sid = player["steam_id"]
            if sid and sid in hero_stats:
                player["heroes"] = hero_stats[sid]
                total  = sum(h["picks"] for h in player["heroes"])
                wins   = sum(h["wins"]  for h in player["heroes"])
                player["total_games"] = total
                player["total_wins"]  = wins
                player["overall_wr"]  = round(wins/total*100, 1) if total else 0
            else:
                player["heroes"]      = []
                player["total_games"] = 0
                player["total_wins"]  = 0
                player["overall_wr"]  = 0

    # 5. Сохраняем
    output = {
        "updated_at":    datetime.now(timezone.utc).isoformat(),
        "tournament_id": TOURNAMENT_ID,
        "matches_total": len(all_games),
        "matches_done":  len(completed),
        "teams":         teams_out,
    }
    content_str = json.dumps(output, ensure_ascii=False, indent=2, default=str)

    with open("teams_data.json", "w", encoding="utf-8") as f:
        f.write(content_str)
    print("✓ teams_data.json сохранён")

    upload_to_gist("teams_data.json", content_str)

if __name__ == "__main__":
    main()
