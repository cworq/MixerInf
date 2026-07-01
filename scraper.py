#!/usr/bin/env python3
"""
mixer-cup.gg Parser — GitHub Actions version (headless)
- Парсит команды: названия, игроков, Steam ID, рейтинги, место в турнире
- Сохраняет ТОЛЬКО teams_data.json (HTML — отдельный статический файл index.html,
  который сам подгружает этот JSON через fetch)

Запуск (локально или в CI): python scraper.py
"""

import asyncio, json, re
from pathlib import Path
from playwright.async_api import async_playwright

OUTPUT_FILE = "teams_data.json"

def wj(path, data):
    Path(path).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

def clean_team_name(raw):
    cleaned = re.sub(
        r'^[A-Z]\s+G\s+\d+\s+W\s+\d+\s+L\s+\d+\s+\S+\s+[A-Z]\s*',
        '', raw.strip()
    )
    return cleaned.strip() or raw.strip()

def steam_id_from_avatar(url):
    m = re.search(r'/avatars/(7656119\d{10})\.', url or "")
    return m.group(1) if m else None

def find_steam_id(text):
    m = re.search(r'7656119\d{10}', str(text))
    return m.group(0) if m else None

def dotabuff_id(steam_id64):
    try:
        return str(int(steam_id64) - 76561197960265728)
    except:
        return None

def log(msg): print(f"  -> {msg}", flush=True)

def parse_team_api(body):
    try:
        team_obj = body.get("data", {}).get("team", {})
        if not team_obj:
            return None, [], {}
        name  = team_obj.get("name", "")
        stats = team_obj.get("stats", {}) or {}
        team_info = {
            "place":       team_obj.get("place"),
            "games_won":   stats.get("gamesWon"),
            "games_lost":  stats.get("gamesLost"),
            "games_total": stats.get("gamesTotal"),
        }
        players = []
        for p in team_obj.get("players", []):
            if not isinstance(p, dict):
                continue
            nick   = p.get("nickname") or p.get("name") or "?"
            rating = p.get("rating")
            rank   = p.get("leaderboardRank")
            avatar = p.get("steamAvatar") or ""
            sid    = steam_id_from_avatar(avatar)
            if not sid:
                for v in p.values():
                    sid = find_steam_id(v)
                    if sid: break
            db_id = dotabuff_id(sid) if sid else None
            players.append({
                "nickname":         nick,
                "steam_id":         sid,
                "steam_url":        f"https://steamcommunity.com/profiles/{sid}" if sid else "",
                "dotabuff_url":     f"https://www.dotabuff.com/players/{db_id}/heroes" if db_id else "",
                "rating":           int(rating) if rating is not None else None,
                "leaderboard_rank": int(rank) if rank is not None else None,
            })
        return name, players[:5], team_info
    except Exception as e:
        log(f"parse error: {e}")
        return None, [], {}


async def scrape_team(page, team):
    fresh = []
    async def on_resp(resp):
        ct = resp.headers.get("content-type", "")
        if ("json" in ct and resp.status == 200
                and "mixer-cup" in resp.url
                and "yandex" not in resp.url):
            try: fresh.append(await resp.json())
            except: pass
    page.on("response", on_resp)
    try:
        await page.goto(team["url"], wait_until="networkidle", timeout=35000)
        # Extra wait for CI runners (slower than local machine)
        await page.wait_for_timeout(4000)
    except Exception as e:
        log(f"goto error: {e}")
    # Give pending XHR/fetch calls a bit more time to complete
    await page.wait_for_timeout(1500)
    page.remove_listener("response", on_resp)

    for body in fresh:
        api_name, players, team_info = parse_team_api(body)
        if players:
            log(f"API: {len(players)} players")
            return api_name or team["name"], players, team_info

    # Try __NEXT_DATA__
    try:
        nd = await page.evaluate("""
            () => { const e = document.getElementById('__NEXT_DATA__');
                    return e ? JSON.parse(e.textContent) : null; }
        """)
        if nd:
            api_name, players, team_info = parse_team_api(nd)
            if players:
                return api_name or team["name"], players, team_info
    except: pass

    # No data found - retry once with longer wait
    log("no players found, retrying...")
    await page.wait_for_timeout(5000)
    fresh2 = []
    async def on_resp2(resp):
        ct = resp.headers.get("content-type", "")
        if ("json" in ct and resp.status == 200
                and "mixer-cup" in resp.url
                and "yandex" not in resp.url):
            try: fresh2.append(await resp.json())
            except: pass
    page.on("response", on_resp2)
    try:
        await page.goto(team["url"], wait_until="networkidle", timeout=35000)
        await page.wait_for_timeout(5000)
    except Exception as e:
        log(f"retry goto error: {e}")
    page.remove_listener("response", on_resp2)

    for body in fresh2:
        api_name, players, team_info = parse_team_api(body)
        if players:
            log(f"API (retry): {len(players)} players")
            return api_name or team["name"], players, team_info

    log("no players found after retry")
    return team["name"], [], {}


async def main():
    print("Mixer-cup.gg Parser (headless / CI)", flush=True)
    print("=" * 50, flush=True)
    all_teams = []

    async with async_playwright() as p:
        # headless=True — без GUI, подходит для GitHub Actions
        browser = await p.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled"],
        )
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            viewport={"width": 1440, "height": 900},
            locale="ru-RU",
        )
        await context.add_init_script(
            "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"
        )
        page = await context.new_page()

        print("\n[1/2] Загружаем список команд...", flush=True)
        await page.goto("https://mixer-cup.gg/ru/active-tour",
                        wait_until="networkidle", timeout=30000)
        await page.wait_for_timeout(3000)

        els = await page.query_selector_all("a[href*='/team/']")
        log(f"Найдено ссылок: {len(els)}")

        seen  = {}
        teams = []
        for el in els:
            href = (await el.get_attribute("href") or "").strip()
            if "/ru/team/" not in href:
                continue
            m = re.search(r'/team/([0-9a-f-]{36})', href)
            if not m:
                continue
            uuid = m.group(1)
            if uuid in seen:
                continue
            seen[uuid] = True
            text = (await el.inner_text()).strip().replace("\n", " ")
            teams.append({
                "name":       text or href,
                "clean_name": clean_team_name(text or href),
                "url":        f"https://mixer-cup.gg{href}",
                "uuid":       uuid,
                "dom_order":  len(teams),
            })

        log(f"Уникальных команд: {len(teams)}")
        if not teams:
            print("Команды не найдены!", flush=True)
            await browser.close()
            return

        print(f"\nНайдено {len(teams)} команд:", flush=True)
        for i, t in enumerate(teams, 1):
            print(f"  {i:2}. {t['clean_name']}", flush=True)

        # Загружаем старые данные как fallback если парсинг вернул пустых игроков
        existing_by_uuid = {}
        if Path(OUTPUT_FILE).exists():
            try:
                old_json = json.loads(Path(OUTPUT_FILE).read_text(encoding="utf-8"))
                old_teams = old_json.get("teams", []) if isinstance(old_json, dict) else old_json
                existing_by_uuid = {t["uuid"]: t for t in old_teams if t.get("uuid")}
                log(f"Fallback cache: {len(existing_by_uuid)} teams loaded")
            except: pass

        print(f"\n[2/2] Парсим страницы команд...", flush=True)
        for i, team in enumerate(teams, 1):
            print(f"\n  [{i:2}/{len(teams)}] {team['clean_name']}", flush=True)
            api_name, players, team_info = await scrape_team(page, team)
            display_name = api_name if api_name else team["clean_name"]

            # Если игроки не найдены — берём старые данные
            if not players and team["uuid"] in existing_by_uuid:
                cached = existing_by_uuid[team["uuid"]]
                players = cached.get("players", [])
                if not team_info.get("place"):
                    team_info["place"]       = cached.get("place")
                    team_info["games_won"]   = cached.get("games_won")
                    team_info["games_lost"]  = cached.get("games_lost")
                    team_info["games_total"] = cached.get("games_total")
                log(f"Used cached data: {len(players)} players")

            team_rating = sum(p["rating"] for p in players if p.get("rating")) or 0

            all_teams.append({
                "name":        display_name,
                "url":         team["url"],
                "uuid":        team["uuid"],
                "dom_order":   team["dom_order"],
                "team_rating": team_rating,
                "place":       team_info.get("place"),
                "games_won":   team_info.get("games_won"),
                "games_lost":  team_info.get("games_lost"),
                "games_total": team_info.get("games_total"),
                "players":     players,
            })
            log(f"Игроков: {len(players)}, рейтинг: {team_rating:,}")
            for pl in players:
                log(f"  {pl['nickname'][:28]:28s}  {pl.get('steam_id') or '--'}")
            await asyncio.sleep(0.5)

        await browser.close()

    # Добавляем timestamp последнего обновления
    from datetime import datetime, timezone
    output = {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "teams": all_teams,
    }

    wj(OUTPUT_FILE, output)
    print(f"\nСохранено -> {OUTPUT_FILE}", flush=True)

    total_p = sum(len(t["players"]) for t in all_teams)
    total_s = sum(1 for t in all_teams for p in t["players"] if p.get("steam_id"))
    print(f"\nГотово! Команд:{len(all_teams)}  Игроков:{total_p}  Steam ID:{total_s}", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
