import asyncio
import os
import sqlite3
from datetime import datetime, time as dtime
from urllib.parse import quote

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import Message, FSInputFile

import aiohttp
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from playwright.async_api import async_playwright

# ================== НАСТРОЙКИ ==================
BOT_TOKEN = os.getenv("BOT_TOKEN", "PASTE_YOUR_TOKEN_HERE")
# Соответствие символов токенов id в CoinGecko
COINGECKO_IDS = {
    "ZRO": "layerzero",       # реальный ZRO
    "BNB": "binancecoin",
    "USDT": "tether"
}
# ===============================================

# Храним состояние в SQLite (простая встраиваемая база)
DB_PATH = "bot.db"

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
        cur.execute("SELECT user_id,zro,bnb,usdt,fzro,baseline_zro,baseline_bnb,baseline_usdt,created_at,daily_hour,daily_minute FROM profiles WHERE user_id=?", (user_id,))
        row = cur.fetchone()
        if not row: return None
        keys = ["user_id","zro","bnb","usdt","fzro","baseline_zro","baseline_bnb","baseline_usdt","created_at","daily_hour","daily_minute"]
        return dict(zip(keys, row))

def upsert_profile(user_id: int, amounts: dict, baselines: dict, schedule_time: tuple[int,int] | None):
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
            amounts.get("ZRO",0.0),
            amounts.get("BNB",0.0),
            amounts.get("USDT",0.0),
            amounts.get("fZRO",0.0),
            baselines.get("ZRO"),
            baselines.get("BNB"),
            baselines.get("USDT"),
            datetime.utcnow().isoformat(),
            schedule_time[0] if schedule_time else None,
            schedule_time[1] if schedule_time else None,
        ))
        con.commit()

async def fetch_prices(session: aiohttp.ClientSession):
    ids = ",".join(COINGECKO_IDS.values())
    url = f"https://api.coingecko.com/api/v3/simple/price?ids={ids}&vs_currencies=usd"
    async with session.get(url, timeout=20) as r:
        r.raise_for_status()
        data = await r.json()
    # Вернём простую мапу символ -> цена
    return {
        "ZRO": float(data[COINGECKO_IDS["ZRO"]]["usd"]),
        "BNB": float(data[COINGECKO_IDS["BNB"]]["usd"]),
        "USDT": float(data[COINGECKO_IDS["USDT"]]["usd"]),
    }

# ---------- Генерация «скриншота кошелька» через HTML + Playwright ----------
WALLET_TEMPLATE = """
<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Wallet</title>
<style>
  body{font-family: system-ui, -apple-system, Segoe UI, Roboto, Ubuntu, Cantarell, Arial; margin:0; background:#0f1320; color:#fff;}
  .wrap{max-width:420px;margin:0 auto;padding:16px;}
  .card{background:#171c2f;border-radius:16px;padding:16px;box-shadow:0 8px 24px rgba(0,0,0,0.35);}
  .hdr{display:flex;justify-content:space-between;align-items:center;margin-bottom:8px;}
  .title{font-weight:700;font-size:18px;}
  .time{opacity:.7;font-size:12px;}
  .total{font-size:28px;font-weight:700;margin:10px 0 16px}
  .row{display:flex;justify-content:space-between;align-items:center;padding:10px 0;border-bottom:1px solid rgba(255,255,255,.06);}
  .row:last-child{border-bottom:none}
  .sym{font-weight:700}
  .amt{opacity:.85}
  .sub{font-size:12px;opacity:.7}
  .chg.up{color:#6ee787}
  .chg.down{color:#ff6b6b}
  .note{font-size:11px;opacity:.6;margin-top:10px}
</style>
</head>
<body>
<div class="wrap">
  <div class="card">
    <div class="hdr">
      <div class="title">Trust-like Wallet</div>
      <div class="time">{{NOW}}</div>
    </div>
    <div class="total">$ {{TOTAL_USD}}</div>

    <!-- Список активов -->
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
    <div class="note">* ZRO показан дважды: обычный и пустышка (учёт раздельный, в UI оба названы ZRO).</div>
  </div>
</div>
</body>
</html>
"""

async def render_wallet_screenshot(playwright, items:list, total_usd:str) -> str:
    # Подставим значения простейшей «шаблонизацией»
    from jinja2 import Template
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
    # авто-высота
    height = await page.evaluate("document.documentElement.scrollHeight")
    await page.set_viewport_size({"width": 420, "height": height})
    out = "wallet.png"
    await page.screenshot(path=out, full_page=True)
    await ctx.close()
    await browser.close()
    return out

# ---------- Логика бота ----------
dp = Dispatcher()
scheduler = AsyncIOScheduler()

def parse_setup(text: str):
    """
    Пример входа (в любом порядке, регистр не важен):
    ZRO 750.034
    BNB 0.01
    USDT 0
    fZRO 1040
    at 18:30
    """
    amounts = {"ZRO":0.0,"BNB":0.0,"USDT":0.0,"fZRO":0.0}
    hour = minute = None
    # разбираем строки
    for raw in text.splitlines():
        s = raw.strip()
        if not s: continue
        low = s.lower()
        if low.startswith("at "):
            # время HH:MM
            hhmm = low.replace("at","",1).strip()
            try:
                hh, mm = hhmm.split(":")
                hour, minute = int(hh), int(mm)
            except:
                pass
            continue
        parts = s.replace(",",".").split()
        if len(parts) >= 2:
            key = parts[0].upper()
            try:
                val = float(parts[1])
            except:
                continue
            if key in ("ZRO","BNB","USDT","FZRO"):
                amounts["fZRO" if key=="FZRO" else key] = val
    schedule_time = (hour, minute) if hour is not None and minute is not None else None
    return amounts, schedule_time

async def compute_snapshot(user_id:int):
    prof = get_profile(user_id)
    if not prof:
        return None, "Сначала выполните /setup — укажите количества токенов."
    async with aiohttp.ClientSession() as sess:
        prices = await fetch_prices(sess)

    # Текущие цены
    pzro, pbnb, pusdt = prices["ZRO"], prices["BNB"], prices["USDT"]
    # Базовые (на момент /setup)
    bzro, bbnb, busdt = prof["baseline_zro"], prof["baseline_bnb"], prof["baseline_usdt"]

    def pct(now, base):
        if not base or base == 0: return 0.0
        return round((now - base) / base * 100, 2)

    # Считаем строки
    rows = []
    # Порядок: ZRO (норм), BNB, USDT, ZRO (пустышка) — но в UI оба названы как "ZRO"
    entries = [
        ("ZRO", prof["zro"], pzro, pct(pzro, bzro)),
        ("BNB", prof["bnb"], pbnb, pct(pbnb, bbnb)),
        ("USDT", prof["usdt"], pusdt, pct(pusdt, busdt)),
        ("ZRO", prof["fzro"], pzro, pct(pzro, bzro)),  # пустышка, но показываем как ZRO
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
    return {"rows": rows, "total": total_str}, None

async def send_snapshot(m: Message, playwright):
    snap, err = await compute_snapshot(m.from_user.id)
    if err:
        await m.answer(err)
        return
    path = await render_wallet_screenshot(playwright, snap["rows"], snap["total"])
    await m.answer_photo(FSInputFile(path), caption=f"Портфель на {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}")

def schedule_user_job(user_id:int, hour:int, minute:int, bot: Bot, playwright):
    # Отменим прошлые
    for job in scheduler.get_jobs():
        if job.id == f"user-{user_id}":
            job.remove()
    trigger = CronTrigger(hour=hour, minute=minute, timezone="UTC")
    async def job():
        from aiogram.types import Chat
        chat_id = user_id
        # создаём временное сообщение через Bot API
        snap, err = await compute_snapshot(user_id)
        if err:
            await bot.send_message(chat_id, err)
            return
        path = await render_wallet_screenshot(playwright, snap["rows"], snap["total"])
        await bot.send_photo(chat_id, FSInputFile(path), caption=f"Плановый снимок {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}")
    scheduler.add_job(job, trigger, id=f"user-{user_id}")

@dp.message(Command("start"))
async def cmd_start(m: Message):
    await m.answer(
        "Привет! Я делаю скриншоты кошелька с ценами ZRO/BNB/USDT и fZRO.\n\n"
        "<b>Команды:</b>\n"
        "• /setup – отправьте в одном сообщении нужные количества и (опционально) время.\n"
        "  Пример:\n"
        "  ZRO 750.034\n"
        "  BNB 0.01\n"
        "  USDT 0\n"
        "  fZRO 1040\n"
        "  at 18:30\n\n"
        "• /shot – прислать скрин прямо сейчас\n"
        "• /prices – показать текущую оценку и проценты\n"
        "• /time HH:MM – ежедневно слать скрин в указанное UTC-время\n"
    )

@dp.message(Command("setup"))
async def cmd_setup(m: Message):
    # Берём всё сообщение после /setup (или всё сообщение, если просто написали без аргументов)
    text = m.text.split("\n", 1)
    body = text[1] if len(text) > 1 else ""
    if not body.strip():
        await m.answer(
            "Отправьте /setup и в теле сообщения укажите значения построчно.\nПример:\n\n"
            "ZRO 750.034\nBNB 0.01\nUSDT 0\nfZRO 1040\nat 18:30"
        )
        return
    amounts, sched = parse_setup(body)

    # Получим базовые цены на момент настройки
    async with aiohttp.ClientSession() as sess:
        prices = await fetch_prices(sess)

    baselines = {"ZRO": prices["ZRO"], "BNB": prices["BNB"], "USDT": prices["USDT"]}
    upsert_profile(m.from_user.id, amounts, baselines, sched)

    msg = (
        "Сохранено ✅\n"
        f"ZRO: {amounts['ZRO']}\n"
        f"BNB: {amounts['BNB']}\n"
        f"USDT: {amounts['USDT']}\n"
        f"fZRO: {amounts['fZRO']}\n"
        f"Базовые цены (USD): ZRO={baselines['ZRO']:.4f}, BNB={baselines['BNB']:.4f}, USDT={baselines['USDT']:.4f}\n"
    )
    if sched:
        schedule_user_job(m.from_user.id, sched[0], sched[1], m.bot, PLAYWRIGHT)
        msg += f"Плановая отправка ежедневно в {sched[0]:02d}:{sched[1]:02d} UTC."
    else:
        msg += "Плановая отправка не задана (можно командой /time HH:MM)."
    await m.answer(msg)

@dp.message(Command("time"))
async def cmd_time(m: Message):
    parts = m.text.strip().split()
    if len(parts) != 2 or ":" not in parts[1]:
        await m.answer("Укажите время как HH:MM (UTC). Например: /time 18:30")
        return
    hh, mm = parts[1].split(":")
    try:
        hour, minute = int(hh), int(mm)
    except:
        await m.answer("Неверный формат времени.")
        return
    prof = get_profile(m.from_user.id)
    if not prof:
        await m.answer("Сначала выполните /setup с вашими количествами токенов.")
        return
    # Перезапишем расписание, базовые и количества не трогаем
    upsert_profile(
        m.from_user.id,
        {"ZRO": prof["zro"], "BNB": prof["bnb"], "USDT": prof["usdt"], "fZRO": prof["fzro"]},
        {"ZRO": prof["baseline_zro"], "BNB": prof["baseline_bnb"], "USDT": prof["baseline_usdt"]},
        (hour, minute)
    )
    schedule_user_job(m.from_user.id, hour, minute, m.bot, PLAYWRIGHT)
    await m.answer(f"Ок! Ежедневно в {hour:02d}:{minute:02d} UTC буду присылать скрин.")

@dp.message(Command("prices"))
async def cmd_prices(m: Message):
    snap, err = await compute_snapshot(m.from_user.id)
    if err:
        await m.answer(err)
        return
    # Краткий текстовый отчёт
    lines = [f"Итоговая оценка: ${snap['total']}"]
    for r in snap["rows"]:
        sgn = "+" if r["CHANGE_PCT"] >= 0 else ""
        lines.append(f"{r['SYMBOL']}: {r['AMOUNT']} шт • ${r['PRICE']}/шт • ${r['VALUE']} • {sgn}{r['CHANGE_PCT']}%")
    await m.answer("\n".join(lines))

@dp.message(Command("shot"))
async def cmd_shot(m: Message):
    await m.answer("Готовлю скрин…")
    await send_snapshot(m, PLAYWRIGHT)

# ---------- Запуск ----------
PLAYWRIGHT = None

async def on_startup():
    global PLAYWRIGHT
    db_init()
    PLAYWRIGHT = await async_playwright().start()
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
        bot = Bot(BOT_TOKEN, parse_mode="HTML")
        await dp.start_polling(bot)
    finally:
        await on_shutdown()

if __name__ == "__main__":
    asyncio.run(main())
