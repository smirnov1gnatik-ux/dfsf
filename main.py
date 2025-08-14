# main.py — Telegram-бот: Trust Wallet screenshot с шаблоном по ссылке
# Фичи:
# - /template <url> — фон-шаблон (рисуем поверх только значения; n→числа, h/m→время)
# - /tplpos — тонкая подстройка координат (в процентах от размеров картинки)
# - /cleartemplate — убрать шаблон
# - /setup — задать количества ZRO/BNB/USDT/fZRO + опциональное время
# - /shot, /prices, кнопки, плановые снимки (UTC)
# - Цены: Binance → CoinGecko → кэш (устойчиво к 429/5xx)
# - Playwright Chromium ставится при старте (без root)

import asyncio
import json
import os
import sqlite3
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Tuple, Dict, Any, List

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
from PIL import Image
from io import BytesIO
from playwright.async_api import async_playwright

# ================== CONFIG ==================
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN не задан (переменная окружения).")

COINGECKO_IDS = {"ZRO": "layerzero", "BNB": "binancecoin", "USDT": "tether"}

DB_PATH = "bot.db"
CACHE_PATH = Path("prices_cache.json")
CACHE_TTL_SECONDS = 180

PLAYWRIGHT_BROWSERS_PATH = Path(os.getenv("PLAYWRIGHT_BROWSERS_PATH", "/opt/render/.cache/ms-playwright"))
# ============================================


# ---------- Playwright Chromium (без root) ----------
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


# ---------- Formatting helpers ----------
def fmt_amount(x: float, decimals: int = 4) -> str:
    if float(x).is_integer():
        s = f"{x:.0f}"
    else:
        s = f"{x:.{decimals}f}".rstrip("0").rstrip(".")
    return s.replace(".", ",")

def fmt_money(x: float) -> str:
    s = f"{x:,.2f}".replace(",", " ").replace(".", ",")
    return f"{s} $"

def fmt_price(x: float) -> str:
    s = f"{x:,.4f}".replace(",", " ").replace(".", ",")
    return f"{s} $/шт"


# ---------- DB ----------
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
            daily_minute INTEGER,
            tpl_url TEXT,
            tpl_cfg TEXT
        )
        """)
        con.commit()

def db_migrate():
    with sqlite3.connect(DB_PATH) as con:
        cur = con.cursor()
        cur.execute("PRAGMA table_info(profiles)")
        cols = [r[1] for r in cur.fetchall()]
        changed = False
        if "tpl_url" not in cols:
            cur.execute("ALTER TABLE profiles ADD COLUMN tpl_url TEXT")
            changed = True
        if "tpl_cfg" not in cols:
            cur.execute("ALTER TABLE profiles ADD COLUMN tpl_cfg TEXT")
            changed = True
        if changed:
            con.commit()

def get_profile(user_id: int):
    with sqlite3.connect(DB_PATH) as con:
        cur = con.execute("""SELECT user_id,zro,bnb,usdt,fzro,
                             baseline_zro,baseline_bnb,baseline_usdt,
                             created_at,daily_hour,daily_minute,tpl_url,tpl_cfg
                             FROM profiles WHERE user_id=?""", (user_id,))
        row = cur.fetchone()
        if not row: return None
        keys = ["user_id","zro","bnb","usdt","fzro","baseline_zro","baseline_bnb","baseline_usdt","created_at","daily_hour","daily_minute","tpl_url","tpl_cfg"]
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

def set_template_url(user_id: int, url: Optional[str]):
    with sqlite3.connect(DB_PATH) as con:
        con.execute("""
            INSERT INTO profiles (user_id, created_at)
            VALUES (?, ?)
            ON CONFLICT(user_id) DO NOTHING
        """, (user_id, datetime.now(timezone.utc).isoformat()))
        con.execute("UPDATE profiles SET tpl_url=? WHERE user_id=?", (url, user_id))
        con.commit()

def get_template_url(user_id: int) -> Optional[str]:
    with sqlite3.connect(DB_PATH) as con:
        row = con.execute("SELECT tpl_url FROM profiles WHERE user_id=?", (user_id,)).fetchone()
        return row[0] if row and row[0] else None

_DEFAULT_TPL_CFG = {
    # относительные координаты (в процентах от ширины/высоты картинки)
    # подобрано под портретный iPhone-скрин; при необходимости подправь /tplpos
    "balance": {"x": 0.50, "y": 0.235, "fs": 0.060},  # крупная сумма
    "delta":   {"x": 0.50, "y": 0.285, "fs": 0.022},  # ↑ 0,07 $ (+0,12%)
    "rows": [  # строки справа: ZRO, BNB, USDT, fZRO
        {"x": 0.90, "y": 0.460, "fs_main": 0.030, "fs_sub": 0.020},
        {"x": 0.90, "y": 0.563, "fs_main": 0.030, "fs_sub": 0.020},
        {"x": 0.90, "y": 0.664, "fs_main": 0.030, "fs_sub": 0.020},
        {"x": 0.90, "y": 0.765, "fs_main": 0.030, "fs_sub": 0.020},
    ],
    "time": {"x": 0.090, "y": 0.065, "fs": 0.020}     # часы:минуты (h:m)
}

def get_template_cfg(user_id:int) -> dict:
    with sqlite3.connect(DB_PATH) as con:
        row = con.execute("SELECT tpl_cfg FROM profiles WHERE user_id=?", (user_id,)).fetchone()
    if row and row[0]:
        try:
            return json.loads(row[0])
        except Exception:
            return dict(_DEFAULT_TPL_CFG)
    return dict(_DEFAULT_TPL_CFG)

def set_template_cfg(user_id:int, cfg:dict):
    with sqlite3.connect(DB_PATH) as con:
        con.execute("UPDATE profiles SET tpl_cfg=? WHERE user_id=?", (json.dumps(cfg), user_id))
        con.commit()


# ---------- Cache ----------
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
    except Exception:
        pass


# ---------- Prices: Binance → CoinGecko → cache ----------
async def fetch_prices(session: aiohttp.ClientSession) -> Tuple[Dict[str, Any], str]:
    # A) Binance
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
    # B) CoinGecko fallback
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


# ---------- Business logic ----------
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

    entries = [
        ("ZRO", prof["zro"], pzro, pct(pzro, bzro), zro24),
        ("BNB", prof["bnb"], pbnb, pct(pbnb, bbnb), bnb24),
        ("USDT", prof["usdt"], pusdt, pct(pusdt, busdt), usdt24),
        ("ZRO", prof["fzro"], pzro, pct(pzro, bzro), zro24),
    ]

    total_val = 0.0
    weighted_parts = []
    items_for_overlay: List[Dict[str, Any]] = []

    for sym, amount, price, change, h24 in entries:
        value_f = amount * price
        total_val += value_f
        weighted_parts.append((value_f, change))
        items_for_overlay.append({
            "SYMBOL": sym,
            "AMOUNT_F": amount,
            "PRICE_F": price,
            "VALUE_F": round(value_f, 2),
            "CHANGE_PCT": change,
            "H24": h24
        })

    total_pct = 0.0 if total_val <= 0 else round(sum(v/total_val * c for v,c in weighted_parts), 2)

    return {"items": items_for_overlay, "total_usd": total_val, "total_pct": total_pct, "source": source}, None


# ---------- HTML overlay template (фон-картинка + наши цифры) ----------
TEMPLATE_OVERLAY_HTML = """
<!doctype html>
<html lang="ru"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<style>
  html,body{margin:0;height:100%;background:#fff}
  .wrap{
    position:relative;
    width:{{W}}px;height:{{H}}px;
    background-image:url('{{URL}}');
    background-size:cover;background-position:center top;background-repeat:no-repeat;
    font-family:-apple-system,BlinkMacSystemFont,Segoe UI,Roboto,Ubuntu,Cantarell,Arial;
    color:#111;
  }
  .txt{
    position:absolute; transform:translate(-50%,-50%);
    padding:2px 6px; background:rgba(255,255,255,0.96); border-radius:6px;
    line-height:1.12; font-weight:800; white-space:nowrap;
  }
  .sub{ font-weight:500; color:#6b7280; }
  .green{ color:#16a34a; font-weight:700 }
  .red{ color:#dc2626; font-weight:700 }
  .right{ left:{{W}}px; transform:translate(-10px,-50%); text-align:right; background:rgba(255,255,255,0.96) }
</style>
</head><body>
<div class="wrap">
  <!-- крупный баланс -->
  <div class="txt" style="left:{{W*BAL.x}}px;top:{{H*BAL.y}}px;font-size:{{H*BAL.fs}}px">{{BALANCE}}</div>
  <!-- строка изменения -->
  <div class="txt" style="left:{{W*DEL.x}}px;top:{{H*DEL.y}}px;font-size:{{H*DEL.fs}}px"><span class="{{'green' if DELTA_SIGN>=0 else 'red'}}">{{'↑' if DELTA_SIGN>=0 else '↓'}}</span> {{DELTA}}</div>
  <!-- часы:минуты -->
  <div class="txt" style="left:{{W*TIME.x}}px;top:{{H*TIME.y}}px;font-size:{{H*TIME.fs}}px;background:transparent">{{CLOCK}}</div>

  <!-- строки активов справа -->
  {% for r in ROWS %}
    <div class="txt right" style="top:{{H*r.y}}px;font-size:{{H*r.fs_main}}px">{{r.value}}</div>
    <div class="txt right sub" style="top:{{H*(r.y+0.040)}}px;font-size:{{H*r.fs_sub}}px">
      {{r.price}}
      {% if r.h24 is not none %}
        <span class="{{'green' if r.h24>=0 else 'red'}}">{{'+' if r.h24>=0 else ''}}{{r.h24}}%</span>
      {% endif %}
    </div>
  {% endfor %}
</div>
</body></html>
"""

async def render_overlay_on_template(playwright, user_id:int, items:list, total_usd:float, total_pct:float) -> Optional[str]:
    """
    Если у пользователя задан /template URL — рисуем поверх его картинки (замещаем 'n'/'h'/'m' на реальные данные).
    Возвращает путь к PNG, иначе None.
    """
    tpl = get_template_url(user_id)
    if not tpl:
        return None

    cfg = get_template_cfg(user_id)

    # Скачиваем картинку и узнаём её размер
    async with aiohttp.ClientSession() as sess:
        async with sess.get(tpl, timeout=30) as r:
            r.raise_for_status()
            data = await r.read()
    im = Image.open(BytesIO(data)).convert("RGB")
    W, H = im.size

    # Готовим верхние значения
    balance_txt = fmt_money(total_usd)
    # считаем "изменение" как абсолют в $ от базы (условно; знак по total_pct)
    delta_val = total_usd * abs(total_pct) / 100.0
    delta_txt = f"{fmt_money(delta_val)} ({'+' if total_pct>=0 else ''}{total_pct}%)"
    delta_sign = 1 if total_pct >= 0 else -1

    now = datetime.now(timezone.utc)
    clock_txt = f"{now.hour:02d}:{now.minute:02d}"  # подстановка h:m

    # 4 строки активов справа
    rows = []
    for idx, it in enumerate(items[:4]):
        row_cfg = cfg["rows"][idx]
        rows.append({
            "y": row_cfg["y"], "fs_main": row_cfg["fs_main"], "fs_sub": row_cfg["fs_sub"],
            "value": fmt_money(it["VALUE_F"]).replace(" $"," $"),
            "price": fmt_price(it["PRICE_F"]),
            "h24": None if it.get("H24") is None else round(float(it["H24"]), 2)
        })

    html = Template(TEMPLATE_OVERLAY_HTML).render(
        URL=tpl, W=W, H=H,
        BAL=cfg["balance"], DEL=cfg["delta"], TIME=cfg["time"],
        BALANCE=balance_txt, DELTA=delta_txt, DELTA_SIGN=delta_sign, CLOCK=clock_txt,
        ROWS=rows
    )

    path_html = "tpl_overlay.html"
    with open(path_html, "w", encoding="utf-8") as f:
        f.write(html)

    # Скрин в реальном размере шаблона
    browser = await playwright.chromium.launch(headless=True, args=["--no-sandbox"])
    ctx = await browser.new_context(viewport={"width": W, "height": H}, device_scale_factor=2)
    page = await ctx.new_page()
    await page.goto("file://" + os.path.abspath(path_html))
    out = "wallet_tpl.png"
    await page.screenshot(path=out, full_page=True)
    await ctx.close(); await browser.close()
    return out


# ---------- Fallback: Trust-like (если шаблон не задан) ----------
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
  .tabs{display:flex;gap:18px;padding:6px 16px 0;font-weight:800}
  .tabs .on{color:#1f4cff}
  .list{padding:8px 10px 16px}
  .row{display:flex;align-items:center;justify-content:space-between;padding:12px 8px;border-bottom:1px solid #f0f0f0}
  .left{display:flex;align-items:center;gap:10px}
  .name{font-weight:800}
  .pill{display:inline-block;background:var(--pill);color:#3b3b3b;padding:4px 10px;border-radius:999px;font-size:12px;margin-top:4px}
  .right{text-align:right}
  .usd{font-weight:800}
  .sub{font-size:13px;color:var(--muted);margin-top:2px}
  .h24.up{color:var(--green)} .h24.down{color:var(--red)}
</style>
</head><body>
<div class="screen">
  <div class="top">Основной кошелёк ▾</div>
  <div class="balance">
    <div class="bal-num">{{BALANCE}}</div>
    <div class="bal-delta">{{ARROW}} {{BALANCE_DELTA}}</div>
  </div>
  <div class="tabs">
    <div class="on">Криптовалюта</div><div class="off" style="color:#9aa3ad">NFT</div>
  </div>
  <div class="list">
  {% for item in ITEMS %}
    <div class="row">
      <div class="left">
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
</div>
</body></html>
"""

async def render_wallet_fallback(playwright, items:list, total_usd:float, total_pct:float=0.0) -> str:
    mapped = []
    for it in items:
        mapped.append({
            "SYMBOL": it["SYMBOL"],
            "CHAIN": "BNB Smart Chain",
            "VALUE": fmt_money(it["VALUE_F"]).replace(" $",""),
            "PRICE": fmt_price(it["PRICE_F"]),
            "H24": None if it.get("H24") is None else round(float(it["H24"]), 2),
        })
    balance_text = fmt_money(total_usd)
    delta_text = f"{fmt_money(total_usd*abs(total_pct)/100)} (+{total_pct}% от базы)" if total_pct>=0 else f"{fmt_money(total_usd*abs(total_pct)/100)} ({total_pct}% от базы)"
    arrow = "↑" if total_pct>=0 else "↓"

    html = Template(WALLET_TEMPLATE).render(
        BALANCE=balance_text.replace(" $"," $"),
        BALANCE_DELTA=delta_text.replace(" $"," $"),
        ARROW=arrow,
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
    await ctx.close(); await browser.close()
    return out


# ---------- Buttons ----------
def menu_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📸 Скрин сейчас", callback_data="shot_now")],
        [InlineKeyboardButton(text="💵 Цены и %", callback_data="prices_now")],
        [
            InlineKeyboardButton(text="🖼 Установить шаблон", callback_data="template_hint"),
            InlineKeyboardButton(text="⏰ Задать время", callback_data="set_time_utc"),
        ],
    ])


# ---------- Bot ----------
dp = Dispatcher()
PLAYWRIGHT = None
scheduler = AsyncIOScheduler()

@dp.message(Command("start"))
async def cmd_start(m: Message):
    await m.answer(
        "Привет! Я делаю скрины кошелька в стиле Trust Wallet.\n"
        "Теперь можно дать **шаблон по ссылке** — я нарисую поверх только значения (n→числа, h/m→время).\n\n"
        "Команды:\n"
        "/template <url> — задать фон-шаблон\n"
        "/cleartemplate — убрать шаблон\n"
        "/tplpos — показать/настроить позиции\n"
        "/setup — задать количества\n"
        "/shot — скрин сейчас\n"
        "/prices — текстовые цены\n"
        "/time HH:MM — ежедневный снимок (UTC)\n",
        reply_markup=menu_kb()
    )

@dp.message(Command("template"))
async def cmd_template(m: Message):
    parts = m.text.strip().split(maxsplit=1)
    if len(parts) != 2 or not parts[1].lower().startswith(("http://","https://")):
        await m.answer("Укажи ссылку на картинку-шаблон (портрет):\n<code>/template https://...</code>", reply_markup=menu_kb())
        return
    set_template_url(m.from_user.id, parts[1].strip())
    await m.answer("Шаблон сохранён ✅ Теперь при /shot буду рисовать поверх этой картинки.", reply_markup=menu_kb())

@dp.message(Command("cleartemplate"))
async def cmd_cleartemplate(m: Message):
    set_template_url(m.from_user.id, None)
    await m.answer("Шаблон очищен. Использую стандартный рендер.", reply_markup=menu_kb())

@dp.message(Command("tplpos"))
async def cmd_tplpos(m: Message):
    """
    Подстройка координат в процентах. Примеры:
    /tplpos balance 0.50 0.24 0.060
    /tplpos delta 0.50 0.285 0.022
    /tplpos row 1 0.90 0.563 0.030 0.020
    /tplpos time 0.09 0.065 0.020
    """
    parts = m.text.strip().split()
    if len(parts) < 2:
        cfg = get_template_cfg(m.from_user.id)
        await m.answer(f"Текущие позиции:\n<code>{json.dumps(cfg, ensure_ascii=False, indent=2)}</code>", reply_markup=menu_kb())
        return
    cfg = get_template_cfg(m.from_user.id)
    try:
        if parts[1] == "balance" and len(parts) == 5:
            cfg["balance"] = {"x": float(parts[2]), "y": float(parts[3]), "fs": float(parts[4])}
        elif parts[1] == "delta" and len(parts) == 5:
            cfg["delta"]   = {"x": float(parts[2]), "y": float(parts[3]), "fs": float(parts[4])}
        elif parts[1] == "time" and len(parts) == 5:
            cfg["time"]    = {"x": float(parts[2]), "y": float(parts[3]), "fs": float(parts[4])}
        elif parts[1] == "row" and len(parts) == 7:
            idx = int(parts[2])
            while len(cfg["rows"]) <= idx: cfg["rows"].append(dict(cfg["rows"][-1]))
            cfg["rows"][idx] = {"x": float(parts[3]), "y": float(parts[4]), "fs_main": float(parts[5]), "fs_sub": float(parts[6])}
        else:
            raise ValueError
        set_template_cfg(m.from_user.id, cfg)
        await m.answer("Ок, обновил координаты ✅", reply_markup=menu_kb())
    except Exception:
        await m.answer("Неверный формат.\nПримеры:\n/tplpos balance 0.50 0.24 0.060\n/tplpos delta 0.50 0.285 0.022\n/tplpos row 1 0.90 0.563 0.030 0.020\n/tplpos time 0.09 0.065 0.020", reply_markup=menu_kb())

@dp.message(Command("setup"))
async def cmd_setup(m: Message):
    parts = m.text.split("\n", 1)
    body = parts[1] if len(parts) > 1 else ""
    if not body.strip():
        await m.answer(
            "Отправьте /setup и в теле построчно значения.\nПример:\n\n"
            "ZRO 750.034\nBNB 0.01\nUSDT 0\nfZRO 1040\nat 18:30",
            reply_markup=menu_kb()
        ); return

    amounts, sched = parse_setup(body)
    try:
        async with aiohttp.ClientSession() as sess:
            prices, source = await fetch_prices(sess)
    except Exception:
        await m.answer("Не удалось получить цены. Попробуйте ещё раз /setup.", reply_markup=menu_kb()); return

    baselines = {k: prices[k]["price"] for k in ("ZRO","BNB","USDT")}
    upsert_profile(m.from_user.id, amounts, baselines, sched)

    if sched:
        schedule_user_job(m.from_user.id, sched[0], sched[1], m.bot)

    msg = (
        "Сохранено ✅\n"
        f"ZRO: {amounts['ZRO']}\n"
        f"BNB: {amounts['BNB']}\n"
        f"USDT: {amounts['USDT']}\n"
        f"fZRO: {amounts['fZRO']}\n"
        f"Базовые цены (USD): ZRO={baselines['ZRO']:.4f}, BNB={baselines['BNB']:.4f}, USDT={baselines['USDT']:.4f}\n"
        f"Источник цен: {'Binance' if source=='binance' else ('CoinGecko' if source=='coingecko' else 'кэш')}\n"
    )
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

    upsert_profile(
        m.from_user.id,
        {"ZRO": prof["zro"], "BNB": prof["bnb"], "USDT": prof["usdt"], "fZRO": prof["fzro"]},
        {"ZRO": prof["baseline_zro"], "BNB": prof["baseline_bnb"], "USDT": prof["baseline_usdt"]},
        (hour, minute)
    )
    schedule_user_job(m.from_user.id, hour, minute, m.bot)
    await m.answer(f"Ок! Ежедневно в {hour:02d}:{minute:02d} UTC.", reply_markup=menu_kb())

@dp.message(Command("prices"))
async def cmd_prices(m: Message):
    snap, err = await compute_snapshot(m.from_user.id)
    if err:
        await m.answer(err, reply_markup=menu_kb()); return
    total = fmt_money(snap['total_usd'])
    lines = [f"Итог: {total}  ({'+' if snap['total_pct']>=0 else ''}{snap['total_pct']}% от базы)"]
    for r in snap["items"]:
        h24 = f" • 24ч {('+' if (r.get('H24') or 0)>=0 else '')}{round(r.get('H24') or 0,2)}%" if r.get("H24") is not None else ""
        lines.append(f"{r['SYMBOL']}: {fmt_amount(r['AMOUNT_F'])} шт • {fmt_price(r['PRICE_F'])} • {fmt_money(r['VALUE_F'])}{h24}")
    if snap.get("source") == "cache":
        lines.append("⚠️ Цены из кэша.")
    elif snap.get("source") == "binance":
        lines.append("Источник цен: Binance.")
    await m.answer("\n".join(lines), reply_markup=menu_kb())

@dp.message(Command("shot"))
async def cmd_shot(m: Message):
    await m.answer("Готовлю скрин…", reply_markup=menu_kb())
    snap, err = await compute_snapshot(m.from_user.id)
    if err:
        await m.answer(err, reply_markup=menu_kb()); return

    cap = f"Портфель на {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}"

    # 1) Пытаемся отрисовать поверх пользовательского шаблона (по ссылке)
    path_tpl = await render_overlay_on_template(PLAYWRIGHT, m.from_user.id, snap["items"], snap["total_usd"], snap.get("total_pct",0.0))
    if path_tpl:
        await m.answer_photo(FSInputFile(path_tpl), caption=cap, reply_markup=menu_kb())
        return

    # 2) Иначе — fallback Trust-like
    path = await render_wallet_fallback(PLAYWRIGHT, snap["items"], snap["total_usd"], snap.get("total_pct",0.0))
    await m.answer_photo(FSInputFile(path), caption=cap, reply_markup=menu_kb())

# ---------- Callbacks ----------
@dp.callback_query(F.data == "shot_now")
async def cb_shot_now(c: CallbackQuery):
    await c.answer()
    snap, err = await compute_snapshot(c.from_user.id)
    if err:
        await c.message.answer(err, reply_markup=menu_kb()); return

    cap = f"Портфель на {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}"

    path_tpl = await render_overlay_on_template(PLAYWRIGHT, c.from_user.id, snap["items"], snap["total_usd"], snap.get("total_pct",0.0))
    if path_tpl:
        await c.message.answer_photo(FSInputFile(path_tpl), caption=cap, reply_markup=menu_kb()); return

    path = await render_wallet_fallback(PLAYWRIGHT, snap["items"], snap["total_usd"], snap.get("total_pct",0.0))
    await c.message.answer_photo(FSInputFile(path), caption=cap, reply_markup=menu_kb())

@dp.callback_query(F.data == "prices_now")
async def cb_prices_now(c: CallbackQuery):
    await c.answer()
    snap, err = await compute_snapshot(c.from_user.id)
    if err:
        await c.message.answer(err, reply_markup=menu_kb()); return
    total = fmt_money(snap['total_usd'])
    lines = [f"Итог: {total}  ({'+' if snap['total_pct']>=0 else ''}{snap['total_pct']}% от базы)"]
    for r in snap["items"]:
        h24 = f" • 24ч {('+' if (r.get('H24') or 0)>=0 else '')}{round(r.get('H24') or 0,2)}%" if r.get("H24") is not None else ""
        lines.append(f"{r['SYMBOL']}: {fmt_amount(r['AMOUNT_F'])} шт • {fmt_price(r['PRICE_F'])} • {fmt_money(r['VALUE_F'])}{h24}")
    await c.message.answer("\n".join(lines), reply_markup=menu_kb())

@dp.callback_query(F.data == "template_hint")
async def cb_template_hint(c: CallbackQuery):
    await c.answer()
    await c.message.answer("Пришли ссылку на картинку-шаблон:\n<code>/template https://i.postimg.cc/kGvHYDzV/botstepan.jpg</code>\n"
                           "Если надо подвинуть надписи — команда /tplpos.", reply_markup=menu_kb())

@dp.callback_query(F.data == "set_time_utc")
async def cb_set_time(c: CallbackQuery):
    await c.answer()
    await c.message.answer("Укажи время как /time HH:MM (UTC).", reply_markup=menu_kb())


# ---------- Scheduler ----------
def schedule_user_job(user_id:int, hour:int, minute:int, bot: Bot):
    job_id = f"user-{user_id}"
    old = scheduler.get_job(job_id)
    if old: old.remove()
    trigger = CronTrigger(hour=hour, minute=minute, timezone="UTC")
    async def job():
        snap, err = await compute_snapshot(user_id)
        if err:
            await bot.send_message(user_id, err, reply_markup=menu_kb()); return
        cap = f"Плановый снимок {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}"
        path_tpl = await render_overlay_on_template(PLAYWRIGHT, user_id, snap["items"], snap["total_usd"], snap.get("total_pct",0.0))
        if path_tpl:
            await bot.send_photo(user_id, FSInputFile(path_tpl), caption=cap, reply_markup=menu_kb()); return
        path = await render_wallet_fallback(PLAYWRIGHT, snap["items"], snap["total_usd"], snap.get("total_pct",0.0))
        await bot.send_photo(user_id, FSInputFile(path), caption=cap, reply_markup=menu_kb())
    scheduler.add_job(job, trigger, id=job_id)


# ---------- Lifecycle ----------
PLAYWRIGHT = None

async def on_startup():
    global PLAYWRIGHT
    db_init()
    db_migrate()
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
        try:
            await bot.delete_webhook(drop_pending_updates=True)
        except Exception:
            pass
        await dp.start_polling(bot)
    finally:
        await on_shutdown()

if __name__ == "__main__":
    asyncio.run(main())
