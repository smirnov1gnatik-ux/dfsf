# main.py — Trust Wallet-style screenshot bot (aiogram 3.7+)
# Binance→CG→cache, 24h change, проценты от "базы" (/setup),
# Trust UI (иконки, пилюли сетей, формат чисел как на скрине),
# инлайн-кнопки, плановые отправки, Playwright auto-install.

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

# ------------ Config ------------
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN не задан.")

COINGECKO_IDS = {"ZRO": "layerzero", "BNB": "binancecoin", "USDT": "tether"}

# подписи сетей (лейбл под названием монеты)
CHAIN_LABEL = {"ZRO": "BNB Smart Chain", "BNB": "BNB Smart Chain", "USDT": "BNB Smart Chain"}

DB_PATH = "bot.db"
CACHE_PATH = Path("prices_cache.json")
CACHE_TTL_SECONDS = 180
PLAYWRIGHT_BROWSERS_PATH = Path(os.getenv("PLAYWRIGHT_BROWSERS_PATH", "/opt/render/.cache/ms-playwright"))
# ---------------------------------

def ensure_playwright_chromium():
    try:
        if PLAYWRIGHT_BROWSERS_PATH.exists() and any(PLAYWRIGHT_BROWSERS_PATH.rglob("headless_shell")):
            return
    except Exception:
        pass
    try:
        print("Installing Playwright Chromium…")
        subprocess.run(["python", "-m", "playwright", "install", "chromium"], check=True)
    except subprocess.CalledProcessError as e:
        print("Playwright install failed:", e.returncode)

# -------- Helpers: RU formatting --------
def fmt_amount(x: float, decimals: int = 4) -> str:
    # 0.0043 -> "0,0043"; 1040 -> "1040"
    if float(x).is_integer():
        s = f"{x:.0f}"
    else:
        s = f"{x:.{decimals}f}".rstrip("0").rstrip(".")
    return s.replace(".", ",")

def fmt_money(x: float) -> str:
    # 36.06 -> "36,06 $"
    s = f"{x:,.2f}".replace(",", " ").replace(".", ",")
    return f"{s} $"

def fmt_price(x: float) -> str:
    # 2.3 -> "2,3000 $/шт"
    s = f"{x:,.4f}".replace(",", " ").replace(".", ",")
    return f"{s} $/шт"

# -------- DB --------
def db_init():
    with sqlite3.connect(DB_PATH) as con:
        con.execute("""
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
        )""")
        con.commit()

def get_profile(user_id: int):
    with sqlite3.connect(DB_PATH) as con:
        cur = con.execute("""SELECT user_id,zro,bnb,usdt,fzro,
                             baseline_zro,baseline_bnb,baseline_usdt,
                             created_at,daily_hour,daily_minute
                             FROM profiles WHERE user_id=?""", (user_id,))
        row = cur.fetchone()
        if not row: return None
        keys = ["user_id","zro","bnb","usdt","fzro","baseline_zro","baseline_bnb","baseline_usdt","created_at","daily_hour","daily_minute"]
        return dict(zip(keys, row))

def upsert_profile(user_id: int, amounts: dict, baselines: dict, schedule_time: Optional[tuple[int,int]]):
    with sqlite3.connect(DB_PATH) as con:
        con.execute("""
        INSERT INTO profiles (user_id,zro,bnb,usdt,fzro,baseline_zro,baseline_bnb,baseline_usdt,created_at,daily_hour,daily_minute)
        VALUES (?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(user_id) DO UPDATE SET
          zro=excluded.zro,bnb=excluded.bnb,usdt=excluded.usdt,fzro=excluded.fzro,
          baseline_zro=excluded.baseline_zro,baseline_bnb=excluded.baseline_bnb,baseline_usdt=excluded.baseline_usdt,
          created_at=excluded.created_at,daily_hour=excluded.daily_hour,daily_minute=excluded.daily_minute
        """, (
            user_id, float(amounts.get("ZRO",0.0)), float(amounts.get("BNB",0.0)),
            float(amounts.get("USDT",0.0)), float(amounts.get("fZRO",0.0)),
            baselines.get("ZRO"), baselines.get("BNB"), baselines.get("USDT"),
            datetime.now(timezone.utc).isoformat(),
            schedule_time[0] if schedule_time else None, schedule_time[1] if schedule_time else None
        ))
        con.commit()

# -------- Cache --------
def _cache_read() -> Optional[dict]:
    if CACHE_PATH.exists():
        try:
            obj = json.loads(CACHE_PATH.read_text())
            if (datetime.now(timezone.utc).timestamp() - obj.get("ts", 0)) <= CACHE_TTL_SECONDS:
                return obj.get("prices")
        except Exception:
            return None
    return None
def _cache_write(prices: dict):
    try:
        CACHE_PATH.write_text(json.dumps({"ts": datetime.now(timezone.utc).timestamp(), "prices": prices}))
    except Exception: pass

# -------- Prices: Binance → CoinGecko → cache --------
async def fetch_prices(session: aiohttp.ClientSession) -> Tuple[Dict[str, Any], str]:
    # A) Binance (price + 24h change)
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
            prices = {"ZRO":{"price":zro,"h24":zro24}, "BNB":{"price":bnb,"h24":bnb24}, "USDT":{"price":1.0,"h24":0.0}}
            _cache_write({"ZRO":zro,"BNB":bnb,"USDT":1.0})
            return prices, "binance"
    except Exception:
        pass
    # B) CoinGecko (fallback)
    ids = ",".join(COINGECKO_IDS.values())
    url = f"https://api.coingecko.com/api/v3/simple/price?ids={ids}&vs_currencies=usd"
    backoffs = [0.5,1.2,2.5,5.0]
    for i, d in enumerate(backoffs):
        try:
            async with session.get(url, timeout=25) as r:
                if r.status == 429 or 500 <= r.status < 600:
                    raise aiohttp.ClientResponseError(r.request_info, r.history, status=r.status, message="rate/5xx")
                r.raise_for_status()
                data = await r.json()
                prices = {"ZRO":{"price":float(data["layerzero"]["usd"]), "h24":None},
                          "BNB":{"price":float(data["binancecoin"]["usd"]), "h24":None},
                          "USDT":{"price":float(data["tether"]["usd"]), "h24":0.0}}
                _cache_write({"ZRO":prices["ZRO"]["price"],"BNB":prices["BNB"]["price"],"USDT":prices["USDT"]["price"]})
                return prices, "coingecko"
        except aiohttp.ClientResponseError:
            if i < len(backoffs)-1:
                await asyncio.sleep(d); continue
            cached = _cache_read()
            if cached:
                return {"ZRO":{"price":float(cached["ZRO"]),"h24":None},
                        "BNB":{"price":float(cached["BNB"]),"h24":None},
                        "USDT":{"price":float(cached["USDT"]),"h24":0.0}}, "cache"
            raise
        except Exception:
            cached = _cache_read()
            if cached:
                return {"ZRO":{"price":float(cached["ZRO"]),"h24":None},
                        "BNB":{"price":float(cached["BNB"]),"h24":None},
                        "USDT":{"price":float(cached["USDT"]),"h24":0.0}}, "cache"
            raise
    cached = _cache_read()
    if cached:
        return {"ZRO":{"price":float(cached["ZRO"]),"h24":None},
                "BNB":{"price":float(cached["BNB"]),"h24":None},
                "USDT":{"price":float(cached["USDT"]),"h24":0.0}}, "cache"
    raise RuntimeError("Нет источника цен")

# -------- SVG иконки (в т.ч. маленький BSC-бейдж) --------
ICON_BNB = """<svg width="36" height="36" viewBox="0 0 36 36" xmlns="http://www.w3.org/2000/svg">
<circle cx="18" cy="18" r="18" fill="#0B101A"/>
<path d="M18 6l4.8 4.8-2 2L18 10l-2.8 2.8-2-2L18 6zm7.5 7.5l2 2-2 2-2-2 2-2zM18 12.5l3.5 3.5L18 19.5l-3.5-3.5L18 12.5zm-7.5 1l2 2-2 2-2-2 2-2zM18 22l2.8-2.8 2 2L18 28l-4.8-4.8 2-2L18 22z" fill="#F3BA2F"/>
</svg>"""
ICON_USDT = """<svg width="36" height="36" viewBox="0 0 36 36" xmlns="http://www.w3.org/2000/svg">
<circle cx="18" cy="18" r="18" fill="#26A17B"/>
<path fill="#fff" d="M10 12h16v3H19v2.2c2.2.2 3.7.7 3.7 1.1 0 .6-3.4 1.2-7.7 1.2s-7.7-.6-7.7-1.2c0-.4 1.5-.9 3.7-1.1V15H10v-3zm6.3 9.8c4.3 0 7.7-.6 7.7-1.2 0-.4-1.5-.9-3.7-1.1V24h-8v-4.5c-2.2.2-3.7.7-3.7 1.1 0 .6 3.4 1.2 7.7 1.2z"/>
</svg>"""
ICON_ZRO = """<svg width="36" height="36" viewBox="0 0 36 36" xmlns="http://www.w3.org/2000/svg">
<circle cx="18" cy="18" r="18" fill="#6C5CE7"/>
<text x="18" y="22" font-size="12" text-anchor="middle" fill="#fff" font-family="Arial, sans-serif" font-weight="700">ZRO</text>
</svg>"""
# Маленький бейдж BSC поверх иконки
BADGE_BSC = """<svg width="18" height="18" viewBox="0 0 18 18" xmlns="http://www.w3.org/2000/svg">
<circle cx="9" cy="9" r="9" fill="#0B101A"/><path d="M9 2.8l2.1 2.1-0.9.9L9 4.6 7.8 5.8l-0.9-0.9L9 2.8zM12.3 6.1l0.9 0.9-0.9 0.9-0.9-0.9 0.9-0.9zM9 5.6l1.6 1.6L9 8.8 7.4 7.2 9 5.6zM5.7 6.1l0.9 0.9-0.9 0.9-0.9-0.9 0.9-0.9zM9 9.9l1.2-1.2 0.9 0.9L9 12.9 6.9 10.8l0.9-0.9L9 9.9z" fill="#F3BA2F"/>
</svg>"""

def icon_for(symbol:str) -> str:
    if symbol.upper() == "BNB": return ICON_BNB
    if symbol.upper() == "USDT": return ICON_USDT
    return ICON_ZRO  # ZRO и fZRO рисуем одинаково

# -------- Trust Wallet-like HTML --------
WALLET_TEMPLATE = """
<!doctype html>
<html lang="ru"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Trust-like</title>
<style>
  :root{--muted:#6b7280;--green:#16a34a;--red:#dc2626;--pill:#EEF0F4}
  *{box-sizing:border-box}
  body{margin:0;background:#fff;color:#111;font-family:-apple-system,BlinkMacSystemFont,Segoe UI,Roboto,Ubuntu,Cantarell,Arial}
  .screen{width:390px;margin:0 auto;background:#fff}
  .top{display:flex;align-items:center;justify-content:center;height:44px;padding:0 12px;font-size:15px}
  .balance{padding:10px 16px}
  .bal-num{font-size:40px;font-weight:800;letter-spacing:.3px}
  .bal-delta{color:var(--green);font-weight:700;margin-top:4px}
  .actions{display:flex;justify-content:space-between;padding:8px 16px 12px}
  .act{width:78px;height:78px;border-radius:20px;background:#F1F3F7;display:flex;align-items:center;justify-content:center;font-weight:700}
  .tabs{display:flex;gap:18px;padding:6px 16px 0;font-weight:800}
  .tabs .on{color:#1f4cff}
  .list{padding:8px 10px 16px}
  .row{display:flex;align-items:center;justify-content:space-between;padding:12px 8px;border-bottom:1px solid #f0f0f0}
  .left{display:flex;align-items:center;gap:10px}
  .icon-wrap{position:relative;width:36px;height:36px}
  .badge{position:absolute;left:-6px;bottom:-4px}
  .name{font-weight:800}
  .pill{display:inline-block;background:var(--pill);color:#3b3b3b;padding:4px 10px;border-radius:999px;font-size:12px;margin-top:4px}
  .right{text-align:right}
  .usd{font-weight:800}
  .sub{font-size:13px;color:var(--muted);margin-top:2px}
  .h24.up{color:var(--green)} .h24.down{color:var(--red)}
  .manage{padding:18px 16px;color:#0a58ff;font-weight:800}
  .navbar{height:70px;border-top:1px solid #eee}
</style>
</head><body>
<div class="screen">
  <div class="top">Основной кошелёк ▾</div>
  <div class="balance">
    <div class="bal-num">{{BALANCE}}</div>
    <div class="bal-delta">↑ {{BALANCE_DELTA}}</div>
  </div>
  <div class="actions">
    <div class="act">Отправить</div>
    <div class="act">Обмен</div>
    <div class="act" style="background:#1f4cff;color:#fff">Фонд</div>
    <div class="act">Продажа</div>
  </div>
  <div class="tabs">
    <div class="on">Криптовалюта</div><div class="off" style="color:#9aa3ad">NFT</div>
  </div>

  <div class="list">
  {% for item in ITEMS %}
    <div class="row">
      <div class="left">
        <div class="icon-wrap">
          <div class="icon">{{item.ICON|safe}}</div>
          <div class="badge">{{item.BADGE|safe}}</div>
        </div>
        <div>
          <div class="name">{{item.SYMBOL}}</div>
          <div class="pill">{{item.CHAIN}}</div>
        </div>
      </div>
      <div class="right">
        <div class="usd">{{item.VALUE}} $</div>
        <div class="sub">{{item.PRICE}}
          {% if item.H24 is not none %}
          <span class="h24 {{'up' if item.H24>=0 else 'down'}}">{{'+' if item.H24>=0 else ''}}{{item.H24}}%</span>
          {% endif %}
        </div>
      </div>
    </div>
  {% endfor %}
  </div>

  <div class="manage">Управлять криптовалютами</div>
  <div class="navbar"></div>
</div>
</body></html>
"""

# -------- Render --------
async def render_wallet_screenshot(playwright, items:list, total_usd:float, total_pct:float=0.0) -> str:
    # items: [{SYMBOL, AMOUNT_F, PRICE_F, VALUE_F, H24}]
    # Соберём итоговые строки с нужным форматированием
    mapped = []
    for it in items:
        mapped.append({
            "ICON": icon_for(it["SYMBOL"]),
            "BADGE": BADGE_BSC,
            "SYMBOL": it["SYMBOL"],
            "CHAIN": CHAIN_LABEL.get(it["SYMBOL"], "BNB Smart Chain"),
            "VALUE": fmt_money(it["VALUE_F"]).replace(" $",""),  # в шаблоне добавляем " $"
            "PRICE": fmt_price(it["PRICE_F"]),
            "H24": None if it.get("H24") is None else round(float(it["H24"]), 2),
        })

    # шапка баланса как на скрине: число + стрелка, показываем текущий портфель и его % от базы
    balance_text = fmt_money(total_usd)
    delta_text = f"{fmt_money(total_usd * abs(total_pct)/100)} (+{total_pct}% от базы)" if total_pct >= 0 else f"{fmt_money(total_usd * abs(total_pct)/100)} ({total_pct}% от базы)"

    html = Template(WALLET_TEMPLATE).render(
        BALANCE=balance_text.replace(" $"," $"),
        BALANCE_DELTA=delta_text.replace(" $"," $"),
        ITEMS=mapped
    )

    path_html = "wallet.html"
    with open(path_html, "w", encoding="utf-8") as f:
        f.write(html)

    browser = await playwright.chromium.launch(headless=True, args=["--no-sandbox"])
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

# -------- Business logic --------
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
    total_val = 0.0
    weighted_parts = []
    items_for_render = []
    for sym, amount, price, change, h24 in entries:
        value_f = amount * price
        total_val += value_f
        weighted_parts.append((value_f, change))
        items_for_render.append({
            "SYMBOL": sym,
            "AMOUNT_F": amount,
            "PRICE_F": price,
            "VALUE_F": round(value_f, 2),
            "CHANGE_PCT": change,
            "H24": h24
        })

    total_pct = 0.0
    if total_val > 0:
        total_pct = round(sum(v/total_val * c for v, c in weighted_parts), 2)

    return {"items": items_for_render, "total_usd": total_val, "total_pct": total_pct, "source": source}, None

# -------- Scheduler --------
scheduler = AsyncIOScheduler()
def schedule_user_job(user_id:int, hour:int, minute:int, bot: Bot, playwright):
    job_id = f"user-{user_id}"
    old = scheduler.get_job(job_id)
    if old: old.remove()
    trigger = CronTrigger(hour=hour, minute=minute, timezone="UTC")
    async def job():
        snap, err = await compute_snapshot(user_id)
        if err:
            await bot.send_message(user_id, err, reply_markup=menu_kb()); return
        path = await render_wallet_screenshot(PLAYWRIGHT, snap["items"], snap["total_usd"], snap.get("total_pct",0.0))
        cap = f"Плановый снимок {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}"
        src = snap.get("source")
        if src == "cache": cap += " • цены из кэша"
        elif src == "binance": cap += " • цены: Binance"
        await bot.send_photo(user_id, FSInputFile(path), caption=cap, reply_markup=menu_kb())
    scheduler.add_job(job, trigger, id=job_id)

# -------- Buttons --------
def menu_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📸 Скрин сейчас", callback_data="shot_now")],
        [InlineKeyboardButton(text="💵 Цены и %", callback_data="prices_now")],
        [
            InlineKeyboardButton(text="🛠 Шаблон /setup", callback_data="send_setup_template"),
            InlineKeyboardButton(text="⏰ Задать время", callback_data="set_time_utc"),
        ],
    ])

# -------- Bot --------
dp = Dispatcher()
PLAYWRIGHT = None

@dp.message(Command("start"))
async def cmd_start(m: Message):
    await m.answer(
        "Привет! Делаю скрины кошелька в стиле Trust Wallet (ZRO/BNB/USDT и fZRO).\n"
        "Кнопки снизу помогут настроить.",
        reply_markup=menu_kb()
    )

@dp.message(Command("setup"))
async def cmd_setup(m: Message):
    parts = m.text.split("\n", 1)
    body = parts[1] if len(parts) > 1 else ""
    if not body.strip():
        await m.answer("Отправьте /setup и в теле значения.\nПример:\n\n"
                       "ZRO 750.034\nBNB 0.01\nUSDT 0\nfZRO 1040\nat 18:30",
                       reply_markup=menu_kb()); return
    amounts, sched = parse_setup(body)
    try:
        async with aiohttp.ClientSession() as sess:
            prices, source = await fetch_prices(sess)
    except Exception:
        await m.answer("Не удалось получить цены. Попробуйте ещё раз /setup.", reply_markup=menu_kb()); return
    baselines = {k: prices[k]["price"] for k in ("ZRO","BNB","USDT")}
    upsert_profile(m.from_user.id, amounts, baselines, sched)
    if sched: schedule_user_job(m.from_user.id, sched[0], sched[1], m.bot, PLAYWRIGHT)
    msg = ("Сохранено ✅\n"
           f"ZRO: {amounts['ZRO']}\nBNB: {amounts['BNB']}\nUSDT: {amounts['USDT']}\n"
           f"fZRO: {amounts['fZRO']}\n"
           f"Базовые цены (USD): ZRO={prices['ZRO']['price']:.4f}, BNB={prices['BNB']['price']:.4f}, USDT={prices['USDT']['price']:.4f}\n"
           f"Источник цен: {'Binance' if source=='binance' else ('CoinGecko' if source=='coingecko' else 'кэш')}\n")
    msg += f"Плановая отправка ежедневно в {sched[0]:02d}:{sched[1]:02d} UTC." if sched else "Плановая отправка не задана."
    await m.answer(msg, reply_markup=menu_kb())

@dp.message(Command("time"))
async def cmd_time(m: Message):
    parts = m.text.strip().split()
    if len(parts) != 2 or ":" not in parts[1]:
        await m.answer("Укажите время как /time HH:MM (UTC).", reply_markup=menu_kb()); return
    try:
        hh, mm = parts[1].split(":"); hour, minute = int(hh), int(mm)
    except Exception:
        await m.answer("Неверный формат времени.", reply_markup=menu_kb()); return
    prof = get_profile(m.from_user.id)
    if not prof:
        await m.answer("Сначала выполните /setup.", reply_markup=menu_kb()); return
    upsert_profile(m.from_user.id,
        {"ZRO": prof["zro"], "BNB": prof["bnb"], "USDT": prof["usdt"], "fZRO": prof["fzro"]},
        {"ZRO": prof["baseline_zro"], "BNB": prof["baseline_bnb"], "USDT": prof["baseline_usdt"]},
        (hour, minute))
    schedule_user_job(m.from_user.id, hour, minute, m.bot, PLAYWRIGHT)
    await m.answer(f"Ок! Ежедневно в {hour:02d}:{minute:02d} UTC.", reply_markup=menu_kb())

@dp.message(Command("prices"))
async def cmd_prices(m: Message):
    snap, err = await compute_snapshot(m.from_user.id)
    if err: await m.answer(err, reply_markup=menu_kb()); return
    total = fmt_money(snap['total_usd'])
    lines = [f"Итог: {total}  ({'+' if snap['total_pct']>=0 else ''}{snap['total_pct']}% от базы)"]
    for r in snap["items"]:
        h24 = f" • 24ч {('+' if (r.get('H24') or 0)>=0 else '')}{round(r.get('H24') or 0,2)}%" if r.get("H24") is not None else ""
        lines.append(f"{r['SYMBOL']}: {fmt_amount(r['AMOUNT_F'])} шт • {fmt_price(r['PRICE_F'])} • {fmt_money(r['VALUE_F'])}{h24}")
    src = snap.get("source")
    if src == "cache": lines.append("⚠️ Цены из кэша.")
    elif src == "binance": lines.append("Источник цен: Binance.")
    await m.answer("\n".join(lines), reply_markup=menu_kb())

@dp.message(Command("shot"))
async def cmd_shot(m: Message):
    await m.answer("Готовлю скрин…", reply_markup=menu_kb())
    snap, err = await compute_snapshot(m.from_user.id)
    if err: await m.answer(err, reply_markup=menu_kb()); return
    path = await render_wallet_screenshot(PLAYWRIGHT, snap["items"], snap["total_usd"], snap.get("total_pct",0.0))
    cap = f"Портфель на {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}"
    src = snap.get("source")
    if src == "cache": cap += " • цены из кэша"
    elif src == "binance": cap += " • цены: Binance"
    await m.answer_photo(FSInputFile(path), caption=cap, reply_markup=menu_kb())

# ---- Callbacks ----
@dp.callback_query(F.data == "shot_now")
async def cb_shot_now(c: CallbackQuery):
    await c.answer(); await cb_send_shot(c)

@dp.callback_query(F.data == "prices_now")
async def cb_prices_now(c: CallbackQuery):
    await c.answer(); await cb_send_prices(c)

@dp.callback_query(F.data == "send_setup_template")
async def cb_send_setup_template(c: CallbackQuery):
    await c.answer()
    await c.message.answer("Отправьте:\n\n/ setup\nZRO 750.034\nBNB 0.01\nUSDT 0\nfZRO 1040\nat 18:30", reply_markup=menu_kb())

@dp.callback_query(F.data == "set_time_utc")
async def cb_set_time(c: CallbackQuery):
    await c.answer()
    await c.message.answer("Укажите время как /time HH:MM (UTC).", reply_markup=menu_kb())

async def cb_send_shot(c: CallbackQuery):
    snap, err = await compute_snapshot(c.from_user.id)
    if err: await c.message.answer(err, reply_markup=menu_kb()); return
    path = await render_wallet_screenshot(PLAYWRIGHT, snap["items"], snap["total_usd"], snap.get("total_pct",0.0))
    cap = f"Портфель на {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}"
    src = snap.get("source")
    if src == "cache": cap += " • цены из кэша"
    elif src == "binance": cap += " • цены: Binance"
    await c.message.answer_photo(FSInputFile(path), caption=cap, reply_markup=menu_kb())

async def cb_send_prices(c: CallbackQuery):
    snap, err = await compute_snapshot(c.from_user.id)
    if err: await c.message.answer(err, reply_markup=menu_kb()); return
    total = fmt_money(snap['total_usd'])
    lines = [f"Итог: {total}  ({'+' if snap['total_pct']>=0 else ''}{snap['total_pct']}% от базы)"]
    for r in snap["items"]:
        h24 = f" • 24ч {('+' if (r.get('H24') or 0)>=0 else '')}{round(r.get('H24') or 0,2)}%" if r.get("H24") is not None else ""
        lines.append(f"{r['SYMBOL']}: {fmt_amount(r['AMOUNT_F'])} шт • {fmt_price(r['PRICE_F'])} • {fmt_money(r['VALUE_F'])}{h24}")
    src = snap.get("source")
    if src == "cache": lines.append("⚠️ Цены из кэша.")
    elif src == "binance": lines.append("Источник цен: Binance.")
    await c.message.answer("\n".join(lines), reply_markup=menu_kb())

# -------- Lifecycle --------
PLAYWRIGHT = None
scheduler = AsyncIOScheduler()

async def on_startup():
    global PLAYWRIGHT
    db_init()
    ensure_playwright_chromium()
    PLAYWRIGHT = await async_playwright().start()
    if not scheduler.running: scheduler.start()

async def on_shutdown():
    global PLAYWRIGHT
    if scheduler.running: scheduler.shutdown(wait=False)
    if PLAYWRIGHT: await PLAYWRIGHT.stop()

async def main():
    await on_startup()
    try:
        bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
        # На всякий случай снимем старый вебхук (если когда-то включали) —
        # это помогает от «конфликтов» при переключении webhook → polling.
        try:
            await bot.delete_webhook(drop_pending_updates=True)
        except Exception:
            pass
        await dp.start_polling(bot)
    finally:
        await on_shutdown()

if __name__ == "__main__":
    asyncio.run(main())
