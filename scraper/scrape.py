"""Скрапер графіка ДТЕК -> Cloudflare Worker (/ingest).

Запускається в GitHub Actions кожні ~10 хвилин. Бере графік для адреси з сайту ДТЕК
(потрібен браузер, бо сайт захищений від ботів) і надсилає його Worker-у.
"""
import asyncio
import json
import os
import sys
import urllib.request
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from dtek_client import DtekClient
from dtek_client.exceptions import DtekClientError

SITE = os.getenv("DTEK_SITE", "krem")
CITY = os.environ["CITY"]
STREET = os.environ["STREET"]
HOUSE = os.environ["HOUSE"]
WORKER_URL = os.environ["WORKER_URL"].rstrip("/")
INGEST_TOKEN = os.environ["INGEST_TOKEN"]

TZ = ZoneInfo("Europe/Kyiv")
BASE_URL = f"https://www.dtek-{SITE}.com.ua"


async def _browser_client(show_browser: bool):
    from curl_cffi.requests import AsyncSession
    from dtek_client.browser_auth import get_cleared_cookies

    schedule_url = f"{BASE_URL}/ua/shutdowns"
    cookies, csrf = await get_cleared_cookies(schedule_url, show_browser=show_browser)
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "Accept-Language": "uk,en;q=0.9",
        "Origin": BASE_URL,
        "Referer": schedule_url,
        "X-Requested-With": "XMLHttpRequest",
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
    }
    if csrf:
        headers["X-CSRF-Token"] = csrf
    session = AsyncSession(timeout=15.0, headers=headers, cookies=cookies, impersonate="chrome120")
    return DtekClient(SITE, ajax_url=f"{BASE_URL}/ua/ajax", session=session)


async def _fetch_with(client):
    async with client as c:
        today = await c.get_today_schedule(CITY, STREET, HOUSE)
        tomorrow = await c.get_tomorrow_schedule(CITY, STREET, HOUSE)
    return today, tomorrow


async def fetch_raw():
    """Спершу звичайний запит, потім браузер (headless), потім браузер із вікном (під xvfb)."""
    errors = []
    try:
        return await _fetch_with(DtekClient(SITE))
    except Exception as e:  # noqa: BLE001
        errors.append(f"plain: {type(e).__name__}")
    for show in (False, True):
        try:
            return await _fetch_with(await _browser_client(show))
        except Exception as e:  # noqa: BLE001
            errors.append(f"browser(show={show}): {type(e).__name__}: {e}")
    raise DtekClientError("; ".join(errors))


def to_intervals(slots):
    """{'1': 'yes'|'no'|'first'|'second'|..., ...} -> [[start_min, end_min, 'off'|'maybe'], ...].

    Слот N = година (N-1):00–N:00; 'no' = відключення, 'first'/'second' = перша/друга
    половина години, 'maybe'/'mfirst'/'msecond' = можливе відключення.
    """
    parts = {
        "no": (0, 60, "off"), "first": (0, 30, "off"), "second": (30, 60, "off"),
        "maybe": (0, 60, "maybe"), "mfirst": (0, 30, "maybe"), "msecond": (30, 60, "maybe"),
    }
    segs = []
    for key, status in slots.items():
        try:
            hour = int(key) - 1
        except (TypeError, ValueError):
            continue
        name = str(getattr(status, "value", status)).lower()
        if name in parts:
            a, b, state = parts[name]
            segs.append([hour * 60 + a, hour * 60 + b, state])
    segs.sort()
    result = []
    for seg in segs:
        if result and result[-1][1] == seg[0] and result[-1][2] == seg[2]:
            result[-1][1] = seg[1]
        else:
            result.append(seg)
    return result


def post(payload: dict) -> None:
    req = urllib.request.Request(
        f"{WORKER_URL}/ingest",
        data=json.dumps(payload).encode(),
        headers={
            "Authorization": f"Bearer {INGEST_TOKEN}",
            "Content-Type": "application/json",
            "User-Agent": "dtek-scraper/1.0",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        print("Worker:", r.status, r.read().decode()[:200])


async def main() -> None:
    try:
        today_slots, tomorrow_slots = await fetch_raw()
        if not today_slots:
            raise DtekClientError("Графік для цієї адреси не знайдено (перевірте CITY/STREET/HOUSE)")
        today = datetime.now(TZ).date()
        days = {today.isoformat(): to_intervals(today_slots)}
        if tomorrow_slots:
            days[(today + timedelta(days=1)).isoformat()] = to_intervals(tomorrow_slots)
        payload = {"ok": True, "days": days}
        print("Графік отримано, днів:", len(days))
    except Exception as e:  # noqa: BLE001
        payload = {"ok": False, "error": f"{type(e).__name__}: {e}"}
        print("Помилка:", payload["error"], file=sys.stderr)
    post(payload)


if __name__ == "__main__":
    asyncio.run(main())
