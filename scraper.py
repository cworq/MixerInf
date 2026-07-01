#!/usr/bin/env python3
"""
mixer-cup.gg Parser — GitHub Actions version (headless)
- Парсит команды: названия, игроков, Steam ID, рейтинги, место в турнире
- Сохраняет ТОЛЬКО teams_data.json
"""

import asyncio, json, re, os
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
        await page.goto(team["url"], wait_until="networkidle", timeout=25000)
        await page.wait_for_timeout(2000)
    except Exception as e:
        log(f"goto error: {e}")
    page.remove_listener("response", on_resp)

    for body in fresh:
        api_name, players, team_info = parse_team_api(body)
        if players:
            log(f"API: {len(players)} players")
            return api_name or team["name"], players, team_info
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
    log("no players found")
    return team["name"], [], {}

async def main():
    print("Mixer-cup.gg Parser (headless / CI)", flush=True)
    print("=" * 50, flush=True)
    all_teams = []

    # 1. Считываем и жестко очищаем куки
    my_cookie_raw = os.environ.get("MIXER_COOKIE", "").strip()
    
    if not my_cookie_raw:
        print("⚠️ ВНИМАНИЕ: Секрет MIXER_COOKIE пустой! Проверь настройки GitHub Secrets.", flush=True)
    else:
        # Если случайно скопировал со словом "Cookie:" - скрипт сам это исправит
        if my_cookie_raw.lower().startswith("cookie:"):
            my_cookie_raw = re.sub(r'(?i)^cookie:\s*', '', my_cookie_raw)
        print(f"✅ Секрет MIXER_COOKIE найден (начинается с: {my_cookie_raw[:10]}...). Применяю...", flush=True)

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-setuid-sandbox"
            ],
        )
        
        # 2. Формируем "человеческие" заголовки для Nginx
        extra_headers = {
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
        }
        
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            viewport={"width": 1440, "height": 900},
            locale="ru-RU",
            extra_http_headers=extra_headers
        )

        # 3. Интегрируем куки
        if my_cookie_raw:
            try:
                cookies_list = json.loads(my_cookie_raw)
                for c in cookies_list:
                    if 'sameSite' in c and c['sameSite'] not in ['Strict', 'Lax', 'None']:
                        del c['sameSite']
                await context.add_cookies(cookies_list)
                print("  -> Куки (JSON) успешно интегрированы!", flush=True)
            except json.JSONDecodeError:
                # Если сырая строка, добавляем напрямую, но УЖЕ без слова Cookie:
                extra_headers["Cookie"] = my_cookie_raw
                await context.set_extra_http_headers(extra_headers)
                print("  -> Куки (Сырая строка) очищены и добавлены в заголовки!", flush=True)
            except Exception as e:
                print(f"  ❌ Ошибка при добавлении куков: {e}", flush=True)

        # Базовый антидетект
        await context.add_init_script(
            "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"
        )
        page = await context.new_page()

        print("\n[1/2] Загружаем список команд...", flush=True)
        try:
            response = await page.goto("https://mixer-cup.gg/ru/active-tour",
                            wait_until="domcontentloaded", timeout=45000)
            print(f"  -> Статус ответа сайта: {response.status if response else 'Нет ответа'}", flush=True)
        except Exception as e:
            print(f"❌ Не удалось загрузить главную страницу: {e}", flush=True)
            await browser.close()
            return

        print("  -> Ожидаю рендеринга карточек команд...", flush=True)
        try:
            await page.wait_for_selector("a[href*='/team/']", timeout=15000)
        except Exception:
            print("  ⚠️ Селектор команд не появился. Пробую сделать скролл...", flush=True)

        print("  -> Выполняю эмуляцию прокрутки...", flush=True)
        await page.evaluate("""
            async () => {
                await new Promise((resolve) => {
                    let totalHeight = 0;
                    let distance = 200;
                    let timer = setInterval(() => {
                        let scrollHeight = document.body.scrollHeight;
                        window.scrollBy(0, distance);
                        totalHeight += distance;
                        if(totalHeight >= scrollHeight || totalHeight > 4000){
                            clearInterval(timer);
                            window.scrollTo(0, 0);
                            resolve();
                        }
                    }, 100);
                });
            }
        """)
        await page.wait_for_timeout(2000)

        els = await page.query_selector_all("a[href*='/team/']")
        log(f"Найдено ссылок: {len(els)}")

        if len(els) == 0:
            print("❌ Ссылок не обнаружено. Содержимое страницы (первые 1000 симв):", flush=True)
            content = await page.content()
            print(content[:1000], flush=True)
            if "403 Forbidden" in content or "Cloudflare" in content:
                print("🚨 Бот заблокирован защитой сайта! (Скорее всего куки устарели или IP сервера забанен)", flush=True)

        seen  = {}
        teams = []
        for el in els:
            href = (await el.get_attribute("href") or "").strip()
            if "/team/" not in href:
                continue
            m = re.search(r'/team/([0-9a-f-]{36})', href)
            if not m:
                continue
            uuid =
