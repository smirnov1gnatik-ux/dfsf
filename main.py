from typing import Optional, Tuple
import asyncio
import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Tuple

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
    raise RuntimeError("BOT_TOKEN не задан. Добавьте переменную окружения BOT_TOKEN в настройках Render.")

# CoinGecko IDs
COINGECKO_IDS = {"ZRO": "layerzero", "BNB": "binancecoin", "USDT": "tether"}

DB_PATH = "bot.db"
CACHE_PATH = Path("prices_cache.json")
CACHE_TTL_SECONDS = 180  # 3 минуты кэш на случай 429
# ===============================================

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
            datetime.utcnow().isoformat(),
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

# ---------- Цены с ретраями и кэшем ----------
async def fetch_prices(session: aiohttp.ClientSession) -> Tuple[dict, str]:
    """
    Пытаемся так:
      A) Binance (BNBUSDT, ZROUSDT; USDT=1)
      B) CoinGecko (с ретраями и кэшем)
    Возвращаем (prices, source) где source in {'binance','live','cache'}.
    """
    # --- A) Binance ---
    try:
        async with session.get("https://api.binance.com/api/v3/ticker/price?symbol=BNBUSDT", timeout=15) as r1, \
                   session.get("https://api.binance.com/api/v3/ticker/price?symbol=ZROUSDT", timeout=15) as r2:
            r1.raise_for_status(); r2.raise_for_status()
            bnb = float((await r1.json())["price"])
            zro = float((await r2.json())["price"])
            prices = {"ZRO": zro, "BNB": bnb, "USDT": 1.0}
            _cache_write(prices)
            return prices, "binance"
    except Exception:
        pass  # пойдём в CoinGecko + кэш

    # --- B) CoinGecko с ретраями и кэшем ---
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
                    "ZRO": float(data[COINGECKO_IDS["ZRO"]]["usd"]),
                    "BNB": float(data[COINGECKO_IDS["BNB"]]["usd"]),
                    "USDT": float(data[COINGECKO_IDS["USDT"]]["usd"]),
                }
                _cache_write(prices)
                return prices, "live"
        except aiohttp.ClientResponseError:
            if i < len(backoffs) - 1:
                await asyncio.sleep(delay);  # подождём и попробуем снова
                continue
            cached = _cache_read()
            if cached:
                return cached, "cache"
            raise
        except Exception:
            cached = _cache_read()
            if cached:
                return cached, "cache"
            raise


# ---------- HTML-шаблон «скриншота» ----------
WALLET_TEMPLATE = """
<!doctype html>
<html lang="ru"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Wallet</title>
<style>
  body{font-family:system-ui,-apple-system,Segoe UI,Roboto,Ubuntu,Cantarell,Arial;margin:0;background:#0f1320;color:#fff}
  .wrap{max-width:420px;margin:0 auto;padding:16px}
  .card{background:#171c2f;border-radius:16px;padding:16px;box-shadow:0 8px 24px rgba(0,0,0,.35)}
  .hdr{display:flex;justify-content:space-between;align-items:center;margin-bottom:8px}
  .title{font-weight:700;font-size:18px}
  .time{opacity:.7;font-size:12px}
  .total{font-size:28px;font-weight:700;margin:10px 0 16px}
  .row{display:flex;justify-content:space-between;align-items:center;padding:10px 0;border-bottom:1px solid rgba(255,255,255,.06)}
  .row:last-child{border-bottom:none}
  .sym{font-weight:700}
  .sub{font-size:12px;opacity:.7}
  .chg.up{color:#6ee787}.chg.down{color:#ff6b6b}
  .note{font-size:11px;opacity:.6;margin-top:10px}
</style>
</head><body>
<div class="wrap">
  <div class="card">
    <div class="hdr">
      <div class="title">Trust-like Wallet</div>
      <div class="time">{{NOW}}</div>
    </div>
    <div class="total">$ {{TOTAL_USD}}</div>
    {% for item in ITEMS %}
    <div class="row">
      <div>
        <div class="sym">{{item.SYMBOL}}</div>
        <div class="sub">{{item.AMOUNT}} • ${{item.PRICE}}/шт</div>
      </div>
      <div style="text-align:right">
        <div>${{item.VALUE}}</div>
        <div class="sub chg {{'up' if item.CHANGE_PCT >= 0 else 'down'}}">
          {{'+' if item.CHANGE_PCT >= 0 else ''}}{{item.CHANGE_PCT}}%
        </div>
      </div>
    </div>
    {% endfor %}
    <div class="note">* ZRO показан дважды: реальный и пустышка (fZRO). В интерфейсе оба названы ZRO, учёт раздельный.</div>
  </div>
</div>
</body></html>
"""

async def render_wallet_screenshot(playwright, items:list, total_usd:str) -> str:
    html = Template(WALLET_TEMPLATE).render(
        NOW=datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC"),
        TOTAL_USD=total_usd,
        ITEMS=items
    )
    path_html = "wallet.html"
    with open(path_html, "w", encoding="utf-8") as f:
        f.write(html)

    browser = await playwright.chromium.launch(headless=True, args=["--no-sandbox"])
    ctx = await browser.new_context(viewport={"width": 420, "height": 800}, device_scale_factor=2)
    page = await ctx.new_page()
    await page.goto("file://" + os.path.abspath(path_html))
    height = await page.evaluate("document.documentElement.scrollHeight")
    await page.set_viewport_size({"width": 420, "height": height})
    out = "wallet.png"
    await page.screenshot(path=out, full_page=True)
    await ctx.close()
    await browser.close()
    return out

# ---------- Логика портфеля ----------
async def compute_snapshot(user_id:int):
    prof = get_profile(user_id)
    if not prof:
        return None, "Сначала выполните /setup — укажите количества токенов."
    async with aiohttp.ClientSession() as sess:
        prices, source = await fetch_prices(sess)

    pzro, pbnb, pusdt = prices["ZRO"], prices["BNB"], prices["USDT"]
    bzro, bbnb, busdt = prof["baseline_zro"], prof["baseline_bnb"], prof["baseline_usdt"]

    def pct(now, base):
        if not base or base == 0:
            return 0.0
        return round((now - base) / base * 100, 2)

    rows = []
    entries = [
        ("ZRO", prof["zro"], pzro, pct(pzro, bzro)),
        ("BNB", prof["bnb"], pbnb, pct(pbnb, bbnb)),
        ("USDT", prof["usdt"], pusdt, pct(pusdt, busdt)),
        ("ZRO", prof["fzro"], pzro, pct(pzro, bzro)),  # fZRO как ZRO
    ]
    total = 0.0
    for sym, amount, price, change in entries:
        value = round(amount * price, 2)
        total += value
        rows.append({
            "SYMBOL": sym,
            "AMOUNT": f"{amount:g}",
            "PRICE": f"{price:,.4f}".replace(",", " "),
            "VALUE": f"{value:,.2f}".replace(",", " "),
            "CHANGE_PCT": change
        })
    total_str = f"{total:,.2f}".replace(",", " ")
    return {"rows": rows, "total": total_str, "source": source}, None

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
        path = await render_wallet_screenshot(PLAYWRIGHT, snap["rows"], snap["total"])
        cap = f"Плановый снимок {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}"
        if snap.get("source") == "cache":
            cap += " • цены из кэша"
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
        "Привет! Я делаю скриншоты кошелька с ценами ZRO/BNB/USDT и fZRO.\n"
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
    except Exception as e:
        # вообще не прерываем: если совсем нет цен — подскажем и выйдем
        await m.answer(f"Не удалось получить цены ни из Binance, ни из CoinGecko. Попробуйте ещё раз /setup.", reply_markup=menu_kb())
        return

    baselines = {"ZRO": prices["ZRO"], "BNB": prices["BNB"], "USDT": prices["USDT"]}
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
        f"Источник цен: {'Binance' if source=='binance' else ('CoinGecko' if source=='live' else 'кэш')}\n"
    )
    msg += f"Плановая отправка ежедневно в {sched[0]:02d}:{sched[1]:02d} UTC." if sched else "Плановая отправка не задана (можно /time HH:MM)."
    await m.answer(msg, reply_markup=menu_kb())


def parse_setup(body: str):
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
    lines = [f"Итоговая оценка: ${snap['total']}"]
    for r in snap["rows"]:
        sgn = "+" if r["CHANGE_PCT"] >= 0 else ""
        lines.append(f"{r['SYMBOL']}: {r['AMOUNT']} шт • ${r['PRICE']}/шт • ${r['VALUE']} • {sgn}{r['CHANGE_PCT']}%")
    if snap.get("source") == "cache":
        lines.append("⚠️ Цены взяты из кэша (CoinGecko временно ограничил запросы).")
    await m.answer("\n".join(lines), reply_markup=menu_kb())

@dp.message(Command("shot"))
async def cmd_shot(m: Message):
    await m.answer("Готовлю скрин…", reply_markup=menu_kb())
    snap, err = await compute_snapshot(m.from_user.id)
    if err:
        await m.answer(err, reply_markup=menu_kb())
        return
    path = await render_wallet_screenshot(PLAYWRIGHT, snap["rows"], snap["total"])
    cap = f"Портфель на {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}"
    if snap.get("source") == "cache":
        cap += " • цены из кэша"
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
    path = await render_wallet_screenshot(PLAYWRIGHT, snap["rows"], snap["total"])
    cap = f"Портфель на {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}"
    if snap.get("source") == "cache":
        cap += " • цены из кэша"
    await c.message.answer_photo(FSInputFile(path), caption=cap, reply_markup=menu_kb())

async def cb_send_prices(c: CallbackQuery):
    snap, err = await compute_snapshot(c.from_user.id)
    if err:
        await c.message.answer(err, reply_markup=menu_kb())
        return
    lines = [f"Итоговая оценка: ${snap['total']}"]
    for r in snap["rows"]:
        sgn = "+" if r["CHANGE_PCT"] >= 0 else ""
        lines.append(f"{r['SYMBOL']}: {r['AMOUNT']} шт • ${r['PRICE']}/шт • ${r['VALUE']} • {sgn}{r['CHANGE_PCT']}%")
    if snap.get("source") == "cache":
        lines.append("⚠️ Цены взяты из кэша (CoinGecko временно ограничил запросы).")
    await c.message.answer("\n".join(lines), reply_markup=menu_kb())

# ---------- Жизненный цикл ----------
PLAYWRIGHT = None

async def on_startup():
    global PLAYWRIGHT
    db_init()
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
