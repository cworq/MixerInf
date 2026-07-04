"""
scraper.py — парсит mixer-cup.gg и загружает данные в GitHub Gist
pip install requests
python scraper.py
"""

import os, re, json, time, sys, urllib.request
from datetime import datetime, timezone
import requests

API_URL        = "https://api.mixer-cup.gg/"
TOURNAMENT_ID  = 26
TEAM_PAGE_BASE = "https://mixer-cup.gg/ru/team/"

HEADERS = {
    "Content-Type": "application/json",
    "Accept": "*/*",
    "Origin": "https://mixer-cup.gg",
    "Referer": "https://mixer-cup.gg/",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
}

Q_TEAMS = """
query Teams($filters: TeamFilterInput!, $first: Int, $offset: Int, $sort: [TeamSortEnum]) {
  teams(first: $first, offset: $offset, filters: $filters, sort: $sort) {
    pageInfo { total totalFiltered }
    items {
      id name number place
      stats { totalWin gamesLost gamesWon gamesTotal upcomingGames }
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

def gql(operation, query, variables=None):
    payload = {"operationName": operation, "query": query, "variables": variables or {}}
    r = requests.post(API_URL, headers=HEADERS, json=payload, timeout=15)
    r.raise_for_status()
    data = r.json()
    if "errors" in data:
        raise ValueError(data["errors"])
    return data.get("data", {})

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
        print(f"  Загружено: {len(all_items)} / {total}", end="\r")
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
        print(f"    [!] Steam данные для {team_id}: {e}")
        return {}

def steam_id_from_avatar(url):
    if not url:
        return None
    m = re.search(r'/avatars/(\d{15,})', url)
    return m.group(1) if m else None

def build_team(raw, order):
    team_id = raw["id"]
    stats   = raw.get("stats") or {}
    players_brief = {p["id"]: p for p in (raw.get("players") or [])}
    players_full  = fetch_team_steam(team_id)
    time.sleep(0.25)

    players = []
    for pid in (list(players_brief.keys()) or list(players_full.keys())):
        brief  = players_brief.get(pid, {})
        full   = players_full.get(pid, {})
        merged = {**brief, **{k: v for k, v in full.items() if v is not None}}
        sid    = steam_id_from_avatar(merged.get("steamAvatar"))
        db_id  = str(int(sid) - 76561197960265728) if sid else None
        players.append({
            "id":          pid,
            "nickname":    merged.get("proName") or merged.get("nickname") or "—",
            "rating":      merged.get("rating"),
            "steam_id":    sid,
            "steam_url":   f"https://steamcommunity.com/profiles/{sid}" if sid else None,
            "dotabuff_url":f"https://www.dotabuff.com/players/{db_id}" if db_id else None,
            "leaderboard": merged.get("leaderboardRank"),
        })

    return {
        "id":          team_id,
        "name":        raw.get("name", "—"),
        "number":      raw.get("number"),
        "place":       raw.get("place"),
        "games_won":   stats.get("gamesWon"),
        "games_lost":  stats.get("gamesLost"),
        "team_rating": sum(p["rating"] or 0 for p in players),
        "dom_order":   raw.get("number") or order,
        "url":         TEAM_PAGE_BASE + team_id,
        "players":     players,
    }

def upload_to_gist(content_str):
    gist_id    = os.environ.get("GIST_ID", "").strip()
    gist_token = os.environ.get("GIST_TOKEN", "").strip()
    if not gist_id or not gist_token:
        print("  (GIST_ID/GIST_TOKEN не заданы — пропускаю загрузку в Gist)")
        return
    print(f"[→] Загружаю в Gist {gist_id}...")
    payload = json.dumps({
        "files": {"teams_data.json": {"content": content_str}}
    }).encode()
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
            print(f"  ✓ Gist обновлён (HTTP {resp.status})")
    except Exception as e:
        print(f"  [!] Ошибка Gist: {e}")

def main():
    print(f"[→] Загружаю команды турнира ID={TOURNAMENT_ID}...")
    teams_raw = fetch_all_teams()
    if not teams_raw:
        print("[!] Команды не найдены.")
        sys.exit(1)

    print(f"  Найдено команд: {len(teams_raw)}\n")
    print("[→] Загружаю Steam данные...")

    results = []
    for i, raw in enumerate(teams_raw):
        print(f"  [{i+1:>2}/{len(teams_raw)}] {raw.get('name','?')}", end="  ", flush=True)
        team = build_team(raw, i)
        results.append(team)
        sid_count = sum(1 for p in team["players"] if p["steam_id"])
        print(f"→  {len(team['players'])} игроков  |  {sid_count} Steam ID  |  Σ {team['team_rating']:,}")

    total_p   = sum(len(t["players"]) for t in results)
    total_sid = sum(sum(1 for p in t["players"] if p["steam_id"]) for t in results)
    print(f"\n  Итого: {len(results)} команд · {total_p} игроков · {total_sid} Steam ID")

    output = {
        "updated_at":    datetime.now(timezone.utc).isoformat(),
        "tournament_id": TOURNAMENT_ID,
        "teams":         results,
    }
    content_str = json.dumps(output, ensure_ascii=False, indent=2, default=str)

    with open("teams_data.json", "w", encoding="utf-8") as f:
        f.write(content_str)
    print("✓ teams_data.json сохранён локально")

    upload_to_gist(content_str)

if __name__ == "__main__":
    main()
