"""
parse_mixer.py  —  генерирует teams_data.json для mixer-cup.gg viewer
pip install requests
python parse_mixer.py
"""

import re, json, time, sys
from datetime import datetime, timezone
import requests

API_URL        = "https://api.mixer-cup.gg/"
TOURNAMENT_ID  = 26          # число, не строка
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

# ─── GraphQL-запросы ──────────────────────────────────────────────────────────

# Список команд (именно такой запрос делает сайт)
Q_TEAMS = """
query Teams($filters: TeamFilterInput!, $first: Int, $offset: Int, $sort: [TeamSortEnum]) {
  teams(first: $first, offset: $offset, filters: $filters, sort: $sort) {
    pageInfo {
      total
      totalFiltered
    }
    items {
      id
      name
      number
      place
      stats {
        totalWin
        gamesLost
        gamesWon
        gamesTotal
        upcomingGames
      }
      players {
        id
        nickname
        rating
        proName
      }
    }
  }
}
"""

# Детали команды — нужны для steamAvatar
Q_TEAM = """
query Team($id: UUID!) {
  team(id: $id) {
    id
    name
    number
    place
    players {
      id
      nickname
      rating
      leaderboardRank
      steamAvatar
      proName
    }
  }
}
"""

# ─── API ──────────────────────────────────────────────────────────────────────

def gql(operation, query, variables=None):
    payload = {
        "operationName": operation,
        "query": query,
        "variables": variables or {},
    }
    r = requests.post(API_URL, headers=HEADERS, json=payload, timeout=15)
    r.raise_for_status()
    data = r.json()
    if "errors" in data:
        raise ValueError(data["errors"])
    return data.get("data", {})


def fetch_all_teams():
    """Постранично загружает все команды турнира."""
    all_items = []
    first  = 30
    offset = 0

    while True:
        data = gql("Teams", Q_TEAMS, {
            "filters": {"tournamentId": TOURNAMENT_ID},
            "sort":    "NUMBER",
            "first":   first,
            "offset":  offset,
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
    """Загружает steamAvatar для игроков команды."""
    try:
        data = gql("Team", Q_TEAM, {"id": team_id})
        return {p["id"]: p for p in (data.get("team") or {}).get("players", [])}
    except Exception as e:
        print(f"    [!] Steam данные для {team_id}: {e}")
        return {}


# ─── Обработка ────────────────────────────────────────────────────────────────

def steam_id_from_avatar(url):
    if not url:
        return None
    m = re.search(r'/avatars/(\d{15,})', url)
    return m.group(1) if m else None


def build_team(raw, order):
    team_id = raw["id"]
    stats   = raw.get("stats") or {}

    # Сначала берём игроков из списочного запроса
    players_brief = {p["id"]: p for p in (raw.get("players") or [])}

    # Докачиваем steamAvatar через Team-запрос
    players_full = fetch_team_steam(team_id)
    time.sleep(0.25)

    players = []
    # Объединяем данные
    all_ids = list(players_brief.keys()) or list(players_full.keys())
    for pid in all_ids:
        brief = players_brief.get(pid, {})
        full  = players_full.get(pid, {})
        merged = {**brief, **{k: v for k, v in full.items() if v is not None}}

        sid = steam_id_from_avatar(merged.get("steamAvatar"))
        players.append({
            "id":          pid,
            "nickname":    merged.get("proName") or merged.get("nickname") or "—",
            "rating":      merged.get("rating"),
            "steam_id":    sid,
            "steam_url":   f"https://steamcommunity.com/profiles/{sid}" if sid else None,
            "dotabuff_url":f"https://www.dotabuff.com/players/{sid}"    if sid else None,
            "leaderboard": merged.get("leaderboardRank"),
        })

    total_rating = sum(p["rating"] or 0 for p in players)

    return {
        "id":          team_id,
        "name":        raw.get("name", "—"),
        "number":      raw.get("number"),
        "place":       raw.get("place"),
        "games_won":   stats.get("gamesWon"),
        "games_lost":  stats.get("gamesLost"),
        "team_rating": total_rating,
        "dom_order":   raw.get("number") or order,
        "url":         TEAM_PAGE_BASE + team_id,
        "players":     players,
    }


# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    print(f"[→] Загружаю команды турнира ID={TOURNAMENT_ID}...")
    teams_raw = fetch_all_teams()

    if not teams_raw:
        print("[!] Команды не найдены.")
        sys.exit(1)

    print(f"  Найдено команд: {len(teams_raw)}\n")
    print("[→] Загружаю Steam данные для каждой команды...")

    results = []
    for i, raw in enumerate(teams_raw):
        name = raw.get("name", "?")
        print(f"  [{i+1:>2}/{len(teams_raw)}] {name}", end="  ", flush=True)
        team = build_team(raw, i)
        results.append(team)
        sid_count = sum(1 for p in team["players"] if p["steam_id"])
        print(f"→  {len(team['players'])} игроков  |  {sid_count} Steam ID  |  Σ {team['team_rating']:,}")

    # Итог
    total_p   = sum(len(t["players"]) for t in results)
    total_sid = sum(sum(1 for p in t["players"] if p["steam_id"]) for t in results)
    print(f"\n  Итого: {len(results)} команд · {total_p} игроков · {total_sid} Steam ID")

    output = {
        "updated_at":     datetime.now(timezone.utc).isoformat(),
        "tournament_id":  TOURNAMENT_ID,
        "teams":          results,
    }

    with open("teams_data.json", "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2, default=str)

    print(f"\n✓ teams_data.json сохранён.")
    print(f"  Запусти: python -m http.server  →  открой http://localhost:8000")


if __name__ == "__main__":
    main()