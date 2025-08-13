# main.py
# ------------------------------------------------------------
# Telegram-бот: Trust Wallet-like screenshot (ZRO/BNB/USDT + fZRO)
# aiogram 3.7+, Playwright; Binance -> CoinGecko -> cache; 24h change;
# инлайн-кнопки; плановые отправки; авто-доустановка Chromium (без root).
# ------------------------------------------------------------

import asyncio
import json
import os
import sqlite3
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Tuple, Dict, Any

import aiohttp
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command
from aiogram.types import (
    Message, FSInputFile, CallbackQuery,
    InlineKeyboardMarkup, InlineKeyboardButton
)
from jinja2 import Template
from playwright.async_api import async_playwright

# ================== НАСТРОЙКИ ==================
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN не задан. Добавьте переменную окружения BOT_TOKEN.")

# Token -> CoinGecko id
COINGECKO_IDS = {"ZRO": "layerzero", "BNB": "binancecoin", "USDT": "tether"}

# Token -> подпись сети (лейбл под названием монеты)
CHAIN_LABEL = {
    "ZRO": "BNB Smart Chain",
    "BNB": "BNB Smart Chain",
    "USDT": "BNB Smart Chain",
}

DB_PATH = "bot.db"
CACHE_PATH = Path("prices_cache.json")
CACHE_TTL_SECONDS = 180  # 3 минуты
PLAYWRIGHT_BROWSERS_PATH = Path(os.getenv("PLAYWRIGHT_BROWSERS_PATH", "/opt/render/.cache/ms-playwright"))
# ===============================================


# ---------- Установка Chromium (без root) ----------
def ensure_playwright_chromium():
    # Если Chromium уже скачан — ничего не делаем
    try:
        if PLAYWRIGHT_BROWSERS_PATH.exists() and any(PLAYWRIGHT_BROWSERS_PATH.rglob("headless_shell")):
            return
    except Exception:
        pass
    # Доустановка браузера (без --with-deps, чтобы не требовался root)
    try:
        print("Installing Playwright Chromium…")
        subprocess.run(["python", "-m", "playwright", "install", "chromium"], check=True)
    except subprocess.CalledProcessError as e:
        print("Playwright install failed with code", e.returncode)


# ---------- База данных ----------
def db_init():
    with sqlite3.connect(DB_PATH) as con:
        cur = con.cursor()
        cur.execute("""
        CREATE TABLE IF NOT EXISTS profiles (
            user_id INTEGER PRIMARY KEY,
            zro REAL DEFAULT 0,
            bnb REAL DEFAULT 0,
            usdt REAL DEFAULT 0,
            fzro REAL DEFAULT 0,
            baseline_zro REAL,
            baseline_bnb REAL,
            baseline_usdt REAL,
            created_at TEXT,
            daily_hour INTEGER,
            daily_minute INTEGER
        )
        """)
        con.commit()

def get_profile(user_id: int):
    with sqlite3.connect(DB_PATH) as con:
        cur = con.cursor()
        cur.execute("""SELECT user_id,zro,bnb,usdt,fzro,
                              baseline_zro,baseline_bnb,baseline_usdt,
                              created_at,daily_hour,daily_minute
                       FROM profiles WHERE user_id=?""", (user_id,))
        row = cur.fetchone()
        if not row:
            return None
        keys = ["user_id","zro","bnb","usdt","fzro","baseline_zro","baseline_bnb","baseline_usdt","created_at","daily_hour","daily_minute"]
        return dict(zip(keys, row))

def upsert_profile(user_id: int, amounts: dict, baselines: dict, schedule_time: Optional[tuple[int,int]]):
    with sqlite3.connect(DB_PATH) as con:
        cur = con.cursor()
        cur.execute("""
        INSERT INTO profiles (user_id,zro,bnb,usdt,fzro,baseline_zro,baseline_bnb,baseline_usdt,created_at,daily_hour,daily_minute)
        VALUES (?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(user_id) DO UPDATE SET
            zro=excluded.zro,
            bnb=excluded.bnb,
            usdt=excluded.usdt,
            fzro=excluded.fzro,
            baseline_zro=excluded.baseline_zro,
            baseline_bnb=excluded.baseline_bnb,
            baseline_usdt=excluded.baseline_usdt,
            created_at=excluded.created_at,
            daily_hour=excluded.daily_hour,
            daily_minute=excluded.daily_minute
        """, (
            user_id,
            float(amounts.get("ZRO",0.0)),
            float(amounts.get("BNB",0.0)),
            float(amounts.get("USDT",0.0)),
            float(amounts.get("fZRO",0.0)),
            baselines.get("ZRO"),
            baselines.get("BNB"),
            baselines.get("USDT"),
            datetime.now(timezone.utc).isoformat(),
            schedule_time[0] if schedule_time else None,
            schedule_time[1] if schedule_time else None,
        ))
        con.commit()


# ---------- Кэш цен ----------
def _cache_read() -> Optional[dict]:
    if CACHE_PATH.exists():
        try:
            obj = json.loads(CACHE_PATH.read_text())
            ts = obj.get("ts", 0)
            if (datetime.now(timezone.utc).timestamp() - ts) <= CACHE_TTL_SECONDS:
                return obj.get("prices")
        except Exception:
            return None
    return None

def _cache_write(prices: dict):
    try:
        CACHE_PATH.write_text(json.dumps({"ts": datetime.now(timezone.utc).timestamp(), "prices": prices}))
    except Exception:
        pass


# ---------- Цены: Binance -> CoinGecko -> кэш ----------
async def fetch_prices(session: aiohttp.ClientSession) -> Tuple[Dict[str, Any], str]:
    """
    Возвращает (prices, source)
    prices = {
      "ZRO": {"price": float, "h24": float|None},
      "BNB": {"price": float, "h24": float|None},
      "USDT": {"price": 1.0,   "h24": 0.0}
    }
    source in {'binance','coingecko','cache'}
    """
    # --- A) Binance (основной источник) ---
    try:
        async with session.get("https://api.binance.com/api/v3/ticker/price?symbol=BNBUSDT", timeout=15) as r1, \
                   session.get("https://api.binance.com/api/v3/ticker/price?symbol=ZROUSDT", timeout=15) as r2, \
                   session.get("https://api.binance.com/api/v3/ticker/24hr?symbol=BNBUSDT", timeout=15) as r3, \
                   session.get("https://api.binance.com/api/v3/ticker/24hr?symbol=ZROUSDT", timeout=15) as r4:
            r1.raise_for_status(); r2.raise_for_status(); r3.raise_for_status(); r4.raise_for_status()
            bnb = float((await r1.json())["price"])
            zro = float((await r2.json())["price"])
            bnb24 = float((await r3.json())["priceChangePercent"])
            zro24 = float((await r4.json())["priceChangePercent"])
            prices = {
                "ZRO": {"price": zro, "h24": zro24},
                "BNB": {"price": bnb, "h24": bnb24},
                "USDT": {"price": 1.0, "h24": 0.0},
            }
            _cache_write({"ZRO": zro, "BNB": bnb, "USDT": 1.0})
            return prices, "binance"
    except Exception:
        pass

    # --- B) CoinGecko (резерв) ---
    ids = ",".join(COINGECKO_IDS.values())
    url = f"https://api.coingecko.com/api/v3/simple/price?ids={ids}&vs_currencies=usd"
    backoffs = [0.5, 1.2, 2.5, 5.0]
    for i, delay in enumerate(backoffs):
        try:
            async with session.get(url, timeout=25) as r:
                if r.status == 429 or 500 <= r.status < 600:
                    raise aiohttp.ClientResponseError(r.request_info, r.history, status=r.status, message="rate/5xx", headers=r.headers)
                r.raise_for_status()
                data = await r.json()
                prices = {
                    "ZRO": {"price": float(data[COINGECKO_IDS["ZRO"]]["usd"]), "h24": None},
                    "BNB": {"price": float(data[COINGECKO_IDS["BNB"]]["usd"]), "h24": None},
                    "USDT": {"price": float(data[COINGECKO_IDS["USDT"]]["usd"]), "h24": 0.0},
                }
                _cache_write({"ZRO": prices["ZRO"]["price"], "BNB": prices["BNB"]["price"], "USDT": prices["USDT"]["price"]})
                return prices, "coingecko"
        except aiohttp.ClientResponseError:
            if i < len(backoffs) - 1:
                await asyncio.sleep(delay)
                continue
            cached = _cache_read()
            if cached:
                return {
                    "ZRO": {"price": float(cached["ZRO"]), "h24": None},
                    "BNB": {"price": float(cached["BNB"]), "h24": None},
                    "USDT": {"price": float(cached["USDT"]), "h24": 0.0},
                }, "cache"
            raise
        except Exception:
            cached = _cache_read()
            if cached:
                return {
                    "ZRO": {"price": float(cached["ZRO"]), "h24": None},
                    "BNB": {"price": float(cached["BNB"]), "h24": None},
                    "USDT": {"price": float(cached["USDT"]), "h24": 0.0},
                }, "cache"
            raise

    # --- C) вообще ничего не вышло ---
    cached = _cache_read()
    if cached:
        return {
            "ZRO": {"price": float(cached["ZRO"]), "h24": None},
            "BNB": {"price": float(cached["BNB"]), "h24": None},
            "USDT": {"price": float(cached["USDT"]), "h24": 0.0},
        }, "cache"
    raise RuntimeError("Нет источника цен")


# ---------- Встроенные SVG-иконки ----------
ICON_BNB = """<svg width="28" height="28" viewBox="0 0 28 28" xmlns="http://www.w3.org/2000/svg">
<rect width="28" height="28" rx="6" fill="#F3BA2F"/>
<path d="M14 5.5l3.7 3.7-1.5 1.5L14 8.5l-2.2 2.2-1.5-1.5L14 5.5zm5.8 5.8l1.5 1.5-1.5 1.5-1.5-1.5 1.5-1.5zM14 11.3l2.7 2.7L14 16.7l-2.7-2.7L14 11.3zm-6.8 0l1.5 1.5-1.5 1.5-1.5-1.5L7.2 11.3zM14 18.5l2.2-2.2 1.5 1.5L14 22.5l-3.7-3.7 1.5-1.5L14 18.5z" fill="#1A1A1A"/>
</svg>"""

ICON_USDT = """<svg width="28" height="28" viewBox="0 0 28 28" xmlns="http://www.w3.org/2000/svg">
<rect width="28" height="28" rx="6" fill="#26A17B"/>
<path fill="#fff" d="M7.5 8.5h13v2.2h-4.9v2c1.8.2 3 .6 3 .9 0 .4-2.7.9-6 .9s-6-.5-6-.9c0-.3 1.2-.7 3-.9v-2H7.5V8.5zm5.1 7.1c3.3 0 6-.4 6-.9 0-.3-1.2-.7-3-.9v4.6h-6v-4.6c-1.8.2-3 .6-3 .9 0 .5 2.7.9 6 .9z"/>
</svg>"""

ICON_ZRO = """<svg width="28" height="28" viewBox="0 0 28 28" xmlns="http://www.w3.org/2000/svg">
<rect width="28" height="28" rx="6" fill="#6C5CE7"/>
<text x="14" y="18" font-size="12" text-anchor="middle" fill="#fff" font-family="Arial, sans-serif" font-weight="700">ZRO</text>
</svg>"""

def icon_for(symbol:str) -> str:
    if symbol.upper() == "BNB":
        return ICON_BNB
    if symbol.upper() == "USDT":
        return ICON_USDT
    return ICON_ZRO  # ZRO и fZRO (рисуем как ZRO)


# ---------- HTML-шаблон Trust Wallet ----------
WALLET_TEMPLATE = """
<!doctype html>
<html lang="ru"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Trust-like</title>
<style>
  :root{--muted:#6b7280;--green:#16a34a;--red:#dc2626}
  *{box-sizing:border-box}
  body{margin:0;background:#fff;color:#111;font-family:-apple-system,BlinkMacSystemFont,Segoe UI,Roboto,Ubuntu,Cantarell,Arial}
  .screen{width:390px;margin:0 auto;background:#fff}
  .topbar{display:flex;align-items:center;justify-content:center;height:44px;padding:0 12px;font-size:15px;color:#111}
  .balance{padding:6px 16px 10px}
  .bal-row{display:flex;align-items:flex-end;gap:8px}
  .bal-value{font-size:36px;font-weight:800;letter-spacing:.3px}
  .pct{font-size:13px;font-weight:700;margin-left:6px}
  .pct.up{color:var(--green)} .pct.down{color:var(--red)}
  .tabs{display:flex;gap:10px;padding:8px 16px 12px}
  .tab{padding:10px 14px;border-radius:12px;background:#f2f6ff;color:#1f4cff;font-weight:700;font-size:13px}
  .list{padding:6px 10px 16px}
  .row{display:flex;align-items:center;justify-content:space-between;padding:12px 8px;border-bottom:1px solid #f0f0f0}
  .left{display:flex;align-items:center;gap:10px}
  .icon{width:28px;height:28px;border-radius:8px;overflow:hidden;display:grid;place-items:center}
  .name{font-weight:700}
  .chain{font-size:12px;color:var(--muted);margin-top:2px}
  .right{text-align:right}
  .usd{font-weight:700}
  .amt{font-size:12px;color:var(--muted)}
  .h24{font-size:12px;margin-left:6px}
  .h24.up{color:var(--green)} .h24.down{color:var(--red)}
</style>
</head><body>
<div class="screen">
  <div class="topbar">Основной кошелёк ▾</div>
  <div class="balance">
    <div class="bal-row">
      <div class="bal-value">$ {{TOTAL_USD}}</div>
      <div class="pct {{'up' if TOTAL_PCT>=0 else 'down'}}">{{'+' if TOTAL_PCT>=0 else ''}}{{TOTAL_PCT}}%</div>
    </div>
  </div>
  <div class="tabs">
    <div class="tab">Фонд</div>
  </div>

  <div class="list">
  {% for item in ITEMS %}
    <div class="row">
      <div class="left">
        <div class="icon">{{item.ICON|safe}}</div>
        <div>
          <div class="name">{{item.SYMBOL}}</div>
          <div class="chain">{{item.CHAIN}}</div>
        </div>
      </div>
      <div class="right">
        <div class="usd">${{item.VALUE}}</div>
        <div class="amt">{{item.AMOUNT}} • ${{item.PRICE}}/шт
          {% if item.H24 is not none %}
          <span class="h24 {{'up' if item.H24>=0 else 'down'}}">{{'+' if item.H24>=0 else ''}}{{item.H24}}% 24ч</span>
          {% endif %}
        </div>
      </div>
    </div>
  {% endfor %}
  </div>
</div>
</body></html>
"""

async def render_wallet_screenshot(playwright, items:list, total_usd:str, total_pct:float=0.0) -> str:
    # items: [{SYMBOL, AMOUNT, PRICE, VALUE, CHANGE_PCT, H24}]
    mapped = []
    for it in items:
        mapped.append({
            "ICON": icon_for(it["SYMBOL"]),
            "SYMBOL": it["SYMBOL"],
            "CHAIN": CHAIN_LABEL.get(it["SYMBOL"], "BNB Smart Chain"),
            "AMOUNT": it["AMOUNT"],
            "PRICE": it["PRICE"],
            "VALUE": it["VALUE"],
            "H24": it.get("H24"),
            "CHANGE_PCT": it["CHANGE_PCT"],
        })

    html = Template(WALLET_TEMPLATE).render(
        TOTAL_USD=total_usd,
        TOTAL_PCT=total_pct,
        ITEMS=mapped
    )

    path_html = "wallet.html"
    with open(path_html, "w", encoding="utf-8") as f:
        f.write(html)

    browser = await playwright.chromium.launch(headless=True, args=["--no-sandbox"])
    # iPhone-ish
    ctx = await browser.new_context(viewport={"width": 390, "height": 844}, device_scale_factor=2)
    page = await ctx.new_page()
    await page.goto("file://" + os.path.abspath(path_html))
    height = await page.evaluate("document.documentElement.scrollHeight")
    await page.set_viewport_size({"width": 390, "height": height})
    out = "wallet.png"
    await page.screenshot(path=out, full_page=True)
    await ctx.close()
    await browser.close()
    return out


# ---------- Бизнес-логика ----------
def parse_setup(body: str):
    """
    Пример:
    ZRO 750.034
    BNB 0.01
    USDT 0
    fZRO 1040
    at 18:30
    """
    amounts = {"ZRO":0.0,"BNB":0.0,"USDT":0.0,"fZRO":0.0}
    hour = minute = None
    for raw in body.splitlines():
        s = raw.strip()
        if not s: 
            continue
        low = s.lower()
        if low.startswith("at "):
            try:
                hh, mm = low.replace("at","",1).strip().split(":")
                hour, minute = int(hh), int(mm)
            except Exception:
                pass
            continue
        parts = s.replace(",", ".").split()
        if len(parts) >= 2:
            key = parts[0].upper()
            try:
                val = float(parts[1])
            except Exception:
                continue
            if key in ("ZRO","BNB","USDT","FZRO"):
                amounts["fZRO" if key=="FZRO" else key] = val
    sched = (hour, minute) if hour is not None and minute is not None else None
    return amounts, sched

async def compute_snapshot(user_id:int):
    prof = get_profile(user_id)
    if not prof:
        return None, "Сначала выполните /setup — укажите количества токенов."

    async with aiohttp.ClientSession() as sess:
        prices, source = await fetch_prices(sess)

    pzro, pbnb, pusdt = prices["ZRO"]["price"], prices["BNB"]["price"], prices["USDT"]["price"]
    zro24, bnb24, usdt24 = prices["ZRO"]["h24"], prices["BNB"]["h24"], prices["USDT"]["h24"]

    bzro, bbnb, busdt = prof["baseline_zro"], prof["baseline_bnb"], prof["baseline_usdt"]

    def pct(now, base):
        if not base or base == 0:
            return 0.0
        return round((now - base) / base * 100, 2)

    rows = []
    # Порядок: ZRO (норм), BNB, USDT, ZRO (пустышка)
    entries = [
        ("ZRO", prof["zro"], pzro, pct(pzro, bzro), zro24),
        ("BNB", prof["bnb"], pbnb, pct(pbnb, bbnb), bnb24),
        ("USDT", prof["usdt"], pusdt, pct(pusdt, busdt), usdt24),
        ("ZRO", prof["fzro"], pzro, pct(pzro, bzro), zro24),
    ]
    total = 0.0
    weighted_parts = []
    for sym, amount, price, change, h24 in entries:
        value_f = amount * price
        value = round(value_f, 2)
        total += value
        weighted_parts.append((value_f, change))
        rows.append({
            "SYMBOL": sym,
            "AMOUNT": f"{amount:g}",
            "PRICE": f"{price:,.4f}".replace(",", " "),
            "VALUE": f"{value:,.2f}".replace(",", " "),
            "CHANGE_PCT": change,
            "H24": None if h24 is None else round(float(h24), 2)
        })

    total_str = f"{total:,.2f}".replace(",", " ")
    # Взвешенный портфельный % относительно "базы"
    total_pct = 0.0
    if total > 0:
        total_pct = round(sum(v/total * c for v, c in weighted_parts), 2)

    return {"rows": rows, "total": total_str, "total_pct": total_pct, "source": source}, None


# ---------- Планировщик ----------
scheduler = AsyncIOScheduler()

def schedule_user_job(user_id:int, hour:int, minute:int, bot: Bot, playwright):
    job_id = f"user-{user_id}"
    old = scheduler.get_job(job_id)
    if old:
        old.remove()

    trigger = CronTrigger(hour=hour, minute=minute, timezone="UTC")

    async def job():
        snap, err = await compute_snapshot(user_id)
        if err:
            await bot.send_message(user_id, err, reply_markup=menu_kb())
            return
        path = await render_wallet_screenshot(PLAYWRIGHT, snap["rows"], snap["total"], snap.get("total_pct", 0.0))
        cap = f"Плановый снимок {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}"
        src = snap.get("source")
        if src == "cache":
            cap += " • цены из кэша"
        elif src == "binance":
            cap += " • цены: Binance"
        await bot.send_photo(user_id, FSInputFile(path), caption=cap, reply_markup=menu_kb())

    scheduler.add_job(job, trigger, id=job_id)


# ---------- Кнопки ----------
def menu_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📸 Скрин сейчас", callback_data="shot_now")],
        [InlineKeyboardButton(text="💵 Цены и %", callback_data="prices_now")],
        [
            InlineKeyboardButton(text="🛠 Шаблон /setup", callback_data="send_setup_template"),
            InlineKeyboardButton(text="⏰ Задать время", callback_data="set_time_utc"),
        ],
    ])


# ---------- Бот ----------
dp = Dispatcher()
PLAYWRIGHT = None  # глобальный инстанс Playwright

@dp.message(Command("start"))
async def cmd_start(m: Message):
    await m.answer(
        "Привет! Я делаю скриншоты кошелька с ценами ZRO/BNB/USDT и fZRO в стиле Trust Wallet.\n"
        "Используйте кнопки ниже или команды:\n"
        "• /setup — задать количества и (опц.) время\n"
        "• /shot — скрин сейчас\n"
        "• /prices — текстовые цены\n"
        "• /time HH:MM — ежедневный скрин по UTC\n",
        reply_markup=menu_kb()
    )

@dp.message(Command("setup"))
async def cmd_setup(m: Message):
    parts = m.text.split("\n", 1)
    body = parts[1] if len(parts) > 1 else ""
    if not body.strip():
        await m.answer(
            "Отправьте /setup и в теле укажите построчно значения.\nПример:\n\n"
            "ZRO 750.034\nBNB 0.01\nUSDT 0\nfZRO 1040\nat 18:30",
            reply_markup=menu_kb()
        )
        return

    amounts, sched = parse_setup(body)
    try:
        async with aiohttp.ClientSession() as sess:
            prices, source = await fetch_prices(sess)
    except Exception:
        await m.answer("Не удалось получить цены. Попробуйте ещё раз /setup.", reply_markup=menu_kb())
        return

    baselines = {k: prices[k]["price"] for k in ("ZRO","BNB","USDT")}
    upsert_profile(m.from_user.id, amounts, baselines, sched)

    if sched:
        schedule_user_job(m.from_user.id, sched[0], sched[1], m.bot, PLAYWRIGHT)

    msg = (
        "Сохранено ✅\n"
        f"ZRO: {amounts['ZRO']}\n"
        f"BNB: {amounts['BNB']}\n"
        f"USDT: {amounts['USDT']}\n"
        f"fZRO: {amounts['fZRO']}\n"
        f"Базовые цены (USD): ZRO={baselines['ZRO']:.4f}, BNB={baselines['BNB']:.4f}, USDT={baselines['USDT']:.4f}\n"
        f"Источник цен: {'Binance' if source=='binance' else ('CoinGecko' if source=='coingecko' else 'кэш')}\n"
    )
    msg += f"Плановая отправка ежедневно в {sched[0]:02d}:{sched[1]:02d} UTC." if sched else "Плановая отправка не задана (можно /time HH:MM)."
    await m.answer(msg, reply_markup=menu_kb())

@dp.message(Command("time"))
async def cmd_time(m: Message):
    parts = m.text.strip().split()
    if len(parts) != 2 or ":" not in parts[1]:
        await m.answer("Укажите время как HH:MM (UTC). Например: /time 18:30", reply_markup=menu_kb())
        return
    try:
        hh, mm = parts[1].split(":")
        hour, minute = int(hh), int(mm)
    except Exception:
        await m.answer("Неверный формат времени.", reply_markup=menu_kb())
        return

    prof = get_profile(m.from_user.id)
    if not prof:
        await m.answer("Сначала выполните /setup с вашими количествами токенов.", reply_markup=menu_kb())
        return

    upsert_profile(
        m.from_user.id,
        {"ZRO": prof["zro"], "BNB": prof["bnb"], "USDT": prof["usdt"], "fZRO": prof["fzro"]},
        {"ZRO": prof["baseline_zro"], "BNB": prof["baseline_bnb"], "USDT": prof["baseline_usdt"]},
        (hour, minute)
    )
    schedule_user_job(m.from_user.id, hour, minute, m.bot, PLAYWRIGHT)
    await m.answer(f"Ок! Ежедневно в {hour:02d}:{minute:02d} UTC буду присылать скрин.", reply_markup=menu_kb())

@dp.message(Command("prices"))
async def cmd_prices(m: Message):
    snap, err = await compute_snapshot(m.from_user.id)
    if err:
        await m.answer(err, reply_markup=menu_kb())
        return
    lines = [f"Итоговая оценка: ${snap['total']}  ({'+' if snap.get('total_pct',0)>=0 else ''}{snap.get('total_pct',0)}% от базы)"]
    for r in snap["rows"]:
        sgn = "+" if r["CHANGE_PCT"] >= 0 else ""
        h24 = f" • 24ч {('+' if (r.get('H24') or 0)>=0 else '')}{r.get('H24')}%" if r.get("H24") is not None else ""
        lines.append(f"{r['SYMBOL']}: {r['AMOUNT']} шт • ${r['PRICE']}/шт • ${r['VALUE']} • {sgn}{r['CHANGE_PCT']}% от базы{h24}")
    src = snap.get("source")
    if src == "cache":
        lines.append("⚠️ Цены взяты из кэша.")
    elif src == "binance":
        lines.append("Источник цен: Binance.")
    await m.answer("\n".join(lines), reply_markup=menu_kb())

@dp.message(Command("shot"))
async def cmd_shot(m: Message):
    await m.answer("Готовлю скрин…", reply_markup=menu_kb())
    snap, err = await compute_snapshot(m.from_user.id)
    if err:
        await m.answer(err, reply_markup=menu_kb())
        return
    path = await render_wallet_screenshot(PLAYWRIGHT, snap["rows"], snap["total"], snap.get("total_pct", 0.0))
    cap = f"Портфель на {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}"
    src = snap.get("source")
    if src == "cache":
        cap += " • цены из кэша"
    elif src == "binance":
        cap += " • цены: Binance"
    await m.answer_photo(FSInputFile(path), caption=cap, reply_markup=menu_kb())

# ---------- Обработчики кнопок ----------
@dp.callback_query(F.data == "shot_now")
async def cb_shot_now(c: CallbackQuery):
    await c.answer()
    await cb_send_shot(c)

@dp.callback_query(F.data == "prices_now")
async def cb_prices_now(c: CallbackQuery):
    await c.answer()
    await cb_send_prices(c)

@dp.callback_query(F.data == "send_setup_template")
async def cb_send_setup_template(c: CallbackQuery):
    await c.answer()
    txt = ("Отправьте одно сообщение формата:\n\n"
           "/setup\n"
           "ZRO 750.034\n"
           "BNB 0.01\n"
           "USDT 0\n"
           "fZRO 1040\n"
           "at 18:30")
    await c.message.answer(txt, reply_markup=menu_kb())

@dp.callback_query(F.data == "set_time_utc")
async def cb_set_time(c: CallbackQuery):
    await c.answer()
    await c.message.answer("Укажите время ежедневной отправки как /time HH:MM (UTC). Например: /time 18:30", reply_markup=menu_kb())

async def cb_send_shot(c: CallbackQuery):
    snap, err = await compute_snapshot(c.from_user.id)
    if err:
        await c.message.answer(err, reply_markup=menu_kb())
        return
    path = await render_wallet_screenshot(PLAYWRIGHT, snap["rows"], snap["total"], snap.get("total_pct", 0.0))
    cap = f"Портфель на {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}"
    src = snap.get("source")
    if src == "cache":
        cap += " • цены из кэша"
    elif src == "binance":
        cap += " • цены: Binance"
    await c.message.answer_photo(FSInputFile(path), caption=cap, reply_markup=menu_kb())

async def cb_send_prices(c: CallbackQuery):
    snap, err = await compute_snapshot(c.from_user.id)
    if err:
        await c.message.answer(err, reply_markup=menu_kb())
        return
    lines = [f"Итоговая оценка: ${snap['total']}  ({'+' if snap.get('total_pct',0)>=0 else ''}{snap.get('total_pct',0)}% от базы)"]
    for r in snap["rows"]:
        sgn = "+" if r["CHANGE_PCT"] >= 0 else ""
        h24 = f" • 24ч {('+' if (r.get('H24') or 0)>=0 else '')}{r.get('H24')}%" if r.get("H24") is not None else ""
        lines.append(f"{r['SYMBOL']}: {r['AMOUNT']} шт • ${r['PRICE']}/шт • ${r['VALUE']} • {sgn}{r['CHANGE_PCT']}% от базы{h24}")
    src = snap.get("source")
    if src == "cache":
        lines.append("⚠️ Цены взяты из кэша.")
    elif src == "binance":
        lines.append("Источник цен: Binance.")
    await c.message.answer("\n".join(lines), reply_markup=menu_kb())


# ---------- Жизненный цикл ----------
PLAYWRIGHT = None

async def on_startup():
    global PLAYWRIGHT
    db_init()
    ensure_playwright_chromium()
    PLAYWRIGHT = await async_playwright().start()
    if not scheduler.running:
        scheduler.start()

async def on_shutdown():
    global PLAYWRIGHT
    if scheduler.running:
        scheduler.shutdown(wait=False)
    if PLAYWRIGHT:
        await PLAYWRIGHT.stop()

async def main():
    await on_startup()
    try:
        bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
        await dp.start_polling(bot)
    finally:
        await on_shutdown()

if __name__ == "__main__":
    asyncio.run(main())
