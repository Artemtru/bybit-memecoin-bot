"""
╔══════════════════════════════════════════════════════╗
║           MANAGER.PY — Адаптивный менеджер v3.2      ║
║                                                      ║
║  v3.2 (2026-10-18):                                  ║
║  • calc_adaptive_qty: исправлены единицы ATR (%)     ║
║    — QTY_USDT снова реально задаёт размер позиции    ║
║  • MIN_SL_PCT, SIDEWAYS_RSI_LONG_MAX/SHORT_MIN       ║
║    вынесены в .env (дефолты = прежнее поведение)     ║
║  • в лог входа добавлен фактический R:R              ║
║  • adapt_rsi_thresholds выключен по умолчанию        ║
║    (ADAPT_RSI=1 чтобы включить)                      ║
║  • global-объявления подняты в начало функций        ║
╚══════════════════════════════════════════════════════╝
"""

import os
import json
import math
import time
import threading
import logging
from decimal import Decimal
from datetime import datetime
from dotenv import load_dotenv
from pybit.unified_trading import HTTP

# FIX: load_dotenv() BEFORE all imports and variable reads
load_dotenv()

from scanner import scan, print_scan_results
import telegram_notifier as tg
import pnl_tracker as tracker
import analytics
import strategy_adapter as adapter

# FIX: HERMES_ENABLED read AFTER load_dotenv()
HERMES_ENABLED   = os.getenv("HERMES_ENABLED", "0") == "1"
brain            = None
strategy_manager = None

if HERMES_ENABLED:
    try:
        import hermes_brain as _brain_module
        import ai_strategy_manager as _asm_module
        brain            = _brain_module
        strategy_manager = _asm_module.create_manager(brain)
        logging.getLogger("hermes_brain").setLevel(logging.INFO)
    except ImportError as e:
        logging.warning(f"Hermes AI недоступен: {e}")
        HERMES_ENABLED = False

# ── Настройки ────────────────────────────────────────
SCAN_INTERVAL_MIN = int(float(os.getenv("SCAN_INTERVAL_MIN", "120")))
MAX_BOTS          = int(float(os.getenv("MAX_BOTS",           "3")))
LEVERAGE          = int(float(os.getenv("LEVERAGE",           "2")))
QTY_USDT_BASE     = float(os.getenv("QTY_USDT",              "20"))
INTERVAL          = os.getenv("INTERVAL", "5")
RSI_PERIOD        = int(float(os.getenv("RSI_PERIOD", "14")))
SLEEP_SEC         = int(float(os.getenv("SLEEP_SEC",  "60")))

MAX_POSITIONS     = int(os.getenv("MAX_POSITIONS",     "5"))
MAX_LEVERAGE      = int(os.getenv("MAX_LEVERAGE",      "2"))
MIN_POSITION_USDT = float(os.getenv("MIN_POSITION_USDT", "10"))
MAX_POSITION_USDT = float(os.getenv("MAX_POSITION_USDT", "30"))

BB_PERIOD = int(float(os.getenv("BB_PERIOD", "20")))
BB_STD    = float(os.getenv("BB_STD",   "1.5"))

ATR_PERIOD  = int(float(os.getenv("ATR_PERIOD",   "14")))
ATR_TP_MULT = float(os.getenv("ATR_TP_MULT",      "2.5"))
ATR_SL_MULT = float(os.getenv("ATR_SL_MULT",      "1.2"))

SIDEWAYS_ATR_THRESHOLD = float(os.getenv("SIDEWAYS_ATR_THRESHOLD", "0.015"))

# v3.2: раньше зашито в коде. Дефолты = прежнее поведение (ничего не меняется,
# пока значения не заданы в .env).
# Минимальная дистанция SL в долях цены (0.005 = 0.5%). На тихих монетах
# (ATR% < ~0.4) именно она определяет SL и TP, а не ATR-множители.
MIN_SL_PCT             = float(os.getenv("MIN_SL_PCT",             "0.005"))
# RSI-фильтр входа в боковике: LONG только при RSI < LONG_MAX, SHORT при RSI > SHORT_MIN
SIDEWAYS_RSI_LONG_MAX  = float(os.getenv("SIDEWAYS_RSI_LONG_MAX",  "55"))
SIDEWAYS_RSI_SHORT_MIN = float(os.getenv("SIDEWAYS_RSI_SHORT_MIN", "45"))
# Автоподстройка RSI_BUY/RSI_SELL по рынку. Эти пороги используются только
# трендовой логикой, которая отключена, поэтому по умолчанию выключено.
ADAPT_RSI              = os.getenv("ADAPT_RSI", "0") == "1"

RSI_BUY_CURRENT  = float(os.getenv("RSI_BUY",  "42"))
RSI_SELL_CURRENT = float(os.getenv("RSI_SELL", "58"))

# FIX 2026-10-15: strategy-risk hardening after a 0/15 winrate night.
# 1) DAILY_LOSS_LIMIT default dropped from -50 to -2 — at QTY~20-30$ and
#    leverage 2x, a single SL is ~$0.07-0.09, so -50 could never realistically
#    trigger (would need ~600 losing trades/day). -2 stops the bleeding after
#    roughly 20-25 consecutive stops instead of letting it run all day.
# 2) STOP_COOLDOWN_MIN — after a symbol stops out, don't re-enter it for N
#    minutes. Without this the bot was re-opening the same symbol into the
#    same adverse move within one SLEEP_SEC tick (e.g. WIFUSDT stopped 3x in
#    ~70 minutes).
# 3) ADX_TREND_THRESHOLD — real ADX is now computed (see calc_adx) and used
#    to veto sideways/mean-reversion entries when the market is actually
#    trending (ADX above threshold). Previously ADX was hardcoded to 25.0
#    and only fed to the (disabled) AI module — never used to gate trades.
DAILY_LOSS_LIMIT    = float(os.getenv("DAILY_LOSS_LIMIT",    "-2"))
STOP_COOLDOWN_MIN    = float(os.getenv("STOP_COOLDOWN_MIN",   "20"))
ADX_PERIOD           = int(float(os.getenv("ADX_PERIOD",      "14")))
ADX_TREND_THRESHOLD  = float(os.getenv("ADX_TREND_THRESHOLD", "25"))

API_KEY    = os.getenv("BYBIT_API_KEY")
API_SECRET = os.getenv("BYBIT_API_SECRET")

# ── Логирование ──────────────────────────────────────
os.makedirs("logs", exist_ok=True)
log_file = f"logs/manager_{datetime.now().strftime('%Y%m%d')}.log"
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(log_file, encoding="utf-8"),
        logging.StreamHandler(),
    ]
)
log = logging.getLogger("manager")

session = HTTP(testnet=False, api_key=API_KEY, api_secret=API_SECRET)

# ── Глобальное состояние ─────────────────────────────
active_bots:       dict[str, threading.Thread] = {}
stop_flags:        dict[str, threading.Event]  = {}
daily_pnl:         float = 0.0
coin_data:         dict[str, dict] = {}
block_new_trades:  bool = False
adapter_counter:   int  = 0
consecutive_stops: int  = 0
STATUS_FILE = "logs/status.json"
# FIX 2026-10-13: tracks the exchange updatedTime of the last closed-PnL row we
# already recorded per symbol, so server-side TP/SL fills (reconciled via
# get_last_closed_pnl) don't get logged twice across ticks/threads.
last_recorded_close_ts: dict[str, int] = {}
# FIX 2026-10-15: per-symbol cooldown after a stop-loss, to stop the bot from
# re-entering the same adverse move within minutes of getting stopped out.
last_stop_time: dict[str, float] = {}


def reload_config():
    """Перечитать параметры из .env без рестарта бота."""
    global LEVERAGE, QTY_USDT_BASE, BB_PERIOD, BB_STD
    global ATR_PERIOD, ATR_TP_MULT, ATR_SL_MULT, SIDEWAYS_ATR_THRESHOLD
    global RSI_BUY_CURRENT, RSI_SELL_CURRENT, SLEEP_SEC, SCAN_INTERVAL_MIN
    global MIN_POSITION_USDT, MAX_POSITION_USDT

    load_dotenv(override=True)
    LEVERAGE               = int(float(os.getenv("LEVERAGE",               str(LEVERAGE))))
    QTY_USDT_BASE          = float(os.getenv("QTY_USDT",                   str(QTY_USDT_BASE)))
    BB_PERIOD              = int(float(os.getenv("BB_PERIOD",               str(BB_PERIOD))))
    BB_STD                 = float(os.getenv("BB_STD",                      str(BB_STD)))
    ATR_TP_MULT            = float(os.getenv("ATR_TP_MULT",                 str(ATR_TP_MULT)))
    ATR_SL_MULT            = float(os.getenv("ATR_SL_MULT",                 str(ATR_SL_MULT)))
    RSI_BUY_CURRENT        = float(os.getenv("RSI_BUY",                     str(RSI_BUY_CURRENT)))
    RSI_SELL_CURRENT       = float(os.getenv("RSI_SELL",                    str(RSI_SELL_CURRENT)))
    SIDEWAYS_ATR_THRESHOLD = float(os.getenv("SIDEWAYS_ATR_THRESHOLD",      str(SIDEWAYS_ATR_THRESHOLD)))
    MIN_POSITION_USDT      = float(os.getenv("MIN_POSITION_USDT",           str(MIN_POSITION_USDT)))
    MAX_POSITION_USDT      = float(os.getenv("MAX_POSITION_USDT",           str(MAX_POSITION_USDT)))
    log.info(
        f"⚙️ Конфиг перезагружен: BB{BB_PERIOD}/{BB_STD}σ "
        f"RSI<{RSI_BUY_CURRENT}/{RSI_SELL_CURRENT} "
        f"TP×{ATR_TP_MULT} SL×{ATR_SL_MULT} QTY={QTY_USDT_BASE}$"
    )


# ═══════════════════════════════════════════════════
#   ТЕХНИЧЕСКИЕ ИНДИКАТОРЫ
# ═══════════════════════════════════════════════════

def get_klines(symbol, interval, limit=150):
    resp    = session.get_kline(category="linear", symbol=symbol, interval=interval, limit=limit)
    candles = list(reversed(resp["result"]["list"]))
    closes  = [float(c[4]) for c in candles]
    highs   = [float(c[2]) for c in candles]
    lows    = [float(c[3]) for c in candles]
    volumes = [float(c[5]) for c in candles]
    return closes, highs, lows, volumes


def calc_rsi(closes, period=14):
    gains, losses = [], []
    for i in range(1, len(closes)):
        d = closes[i] - closes[i - 1]
        gains.append(max(d, 0)); losses.append(max(-d, 0))
    ag = sum(gains[:period]) / period
    al = sum(losses[:period]) / period
    for i in range(period, len(gains)):
        ag = (ag * (period - 1) + gains[i]) / period
        al = (al * (period - 1) + losses[i]) / period
    if al == 0:
        return 100.0
    return round(100 - (100 / (1 + ag / al)), 2)


def calc_bollinger(closes, period=20, std_mult=2.0):
    if len(closes) < period:
        return None, None, None
    w   = closes[-period:]
    mid = sum(w) / period
    std = (sum((x - mid) ** 2 for x in w) / period) ** 0.5
    return round(mid, 8), round(mid + std_mult * std, 8), round(mid - std_mult * std, 8)


def calc_atr(closes, highs, lows, period=14):
    trs = []
    for i in range(1, len(closes)):
        trs.append(max(highs[i]-lows[i], abs(highs[i]-closes[i-1]), abs(lows[i]-closes[i-1])))
    if len(trs) < period:
        return None
    atr = sum(trs[:period]) / period
    for tr in trs[period:]:
        atr = (atr * (period - 1) + tr) / period
    return round(atr, 8)


def calc_ema(closes, period):
    if len(closes) < period:
        return None
    ema = sum(closes[:period]) / period
    k   = 2 / (period + 1)
    for p in closes[period:]:
        ema = p * k + ema * (1 - k)
    return round(ema, 8)


def calc_adx(closes, highs, lows, period=14):
    """Wilder's ADX — real trend-strength indicator.
    FIX 2026-10-15: previously ADX was hardcoded to 25.0 everywhere and never
    actually computed, so the bot had no way to distinguish a trending market
    from a ranging one. This feeds detect_market_mode() to veto mean-reversion
    (sideways) entries when the market is genuinely trending — the "catching
    a falling knife" failure mode behind the 0/15 winrate night.
    """
    n = len(closes)
    if n < period * 2 + 1:
        return None
    plus_dm, minus_dm, trs = [], [], []
    for i in range(1, n):
        up_move   = highs[i] - highs[i - 1]
        down_move = lows[i - 1] - lows[i]
        plus_dm.append(up_move if (up_move > down_move and up_move > 0) else 0.0)
        minus_dm.append(down_move if (down_move > up_move and down_move > 0) else 0.0)
        trs.append(max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1]),
        ))

    def _wilder_smooth(vals, period):
        smoothed = [sum(vals[:period])]
        for v in vals[period:]:
            smoothed.append(smoothed[-1] - (smoothed[-1] / period) + v)
        return smoothed

    atr_s    = _wilder_smooth(trs, period)
    plus_s   = _wilder_smooth(plus_dm, period)
    minus_s  = _wilder_smooth(minus_dm, period)

    dxs = []
    for atr_v, p_v, m_v in zip(atr_s, plus_s, minus_s):
        if atr_v == 0:
            continue
        plus_di  = 100 * (p_v / atr_v)
        minus_di = 100 * (m_v / atr_v)
        denom = plus_di + minus_di
        if denom == 0:
            continue
        dxs.append(100 * abs(plus_di - minus_di) / denom)

    if len(dxs) < period:
        return None
    adx = sum(dxs[:period]) / period
    for dx in dxs[period:]:
        adx = (adx * (period - 1) + dx) / period
    return round(adx, 2)


def detect_market_mode(closes, highs, lows, price):
    """Возвращает (mode, atr, atr_pct, adx). ВАЖНО: atr_pct — в ПРОЦЕНТАХ (0.27 == 0.27%)."""
    atr = calc_atr(closes, highs, lows, ATR_PERIOD)
    if atr is None:
        return "trend", None, None, None
    atr_pct = atr / price
    ema20   = calc_ema(closes, 20)
    ema50   = calc_ema(closes, 50)
    adx     = calc_adx(closes, highs, lows, ADX_PERIOD)
    if ema20 and ema50:
        ema_diff = abs(ema20 - ema50) / price
        mode = "sideways" if (atr_pct < SIDEWAYS_ATR_THRESHOLD and ema_diff < 0.02) else "trend"
    else:
        mode = "sideways" if atr_pct < SIDEWAYS_ATR_THRESHOLD else "trend"
    # FIX 2026-10-15: ADX veto — if the real ADX shows a strong trend
    # (>= ADX_TREND_THRESHOLD), don't trust the sideways/mean-reversion
    # classification even if ATR/EMA looked calm; force "trend" mode so
    # signal_sideways() (which fades moves — the opposite of what you want
    # in a real trend) doesn't get used.
    if adx is not None and adx >= ADX_TREND_THRESHOLD:
        mode = "trend"
    return mode, atr, round(atr_pct * 100, 2), adx


# ═══════════════════════════════════════════════════
#   ТОРГОВЫЕ СИГНАЛЫ
# ═══════════════════════════════════════════════════

def signal_sideways(closes, highs, lows, price, rsi, volumes=None):
    mid, upper, lower = calc_bollinger(closes, BB_PERIOD, BB_STD)
    if mid is None:
        return None, None, None
    prev   = closes[-2]
    vol_ok = True
    if volumes and len(volumes) >= 20:
        avg_vol = sum(volumes[-20:-1]) / 19
        vol_ok  = volumes[-1] > avg_vol * 1.3
    # v3.2: пороги RSI берутся из .env (дефолт 55/45 = прежнее поведение)
    if prev <= lower and price > lower and rsi < SIDEWAYS_RSI_LONG_MAX and vol_ok:
        return "Buy",  mid, lower
    if prev >= upper and price < upper and rsi > SIDEWAYS_RSI_SHORT_MIN and vol_ok:
        return "Sell", mid, upper
    return None, None, None


def signal_trend(rsi):
    # Не используется с 2026-10-18 (торговля в тренде отключена); оставлено для истории.
    if rsi < RSI_BUY_CURRENT:  return "Buy"
    if rsi > RSI_SELL_CURRENT: return "Sell"
    return None


# ═══════════════════════════════════════════════════
#   БИРЖЕВЫЕ ФУНКЦИИ
# ═══════════════════════════════════════════════════

def get_instrument_info(symbol):
    resp      = session.get_instruments_info(category="linear", symbol=symbol)
    info      = resp["result"]["list"][0]
    tick_size = float(info["priceFilter"]["tickSize"])
    qty_step  = float(info["lotSizeFilter"]["qtyStep"])
    min_qty   = float(info["lotSizeFilter"]["minOrderQty"])
    return tick_size, qty_step, min_qty


def round_price(price, tick_size):
    # FIX 2026-10-13: tick_size for low-price coins (SHIB, BONK, DOGE, PEPE, etc.)
    # often arrives as scientific notation (e.g. 1e-05, 1e-06). str(1e-05) == '1e-05'
    # has NO '.', so the old `"." in str(tick_size)` check fell through to prec=0,
    # rounding every sub-$1 coin's price to a whole number (-> 0.0) and killing
    # every TP/SL calc for that symbol ("Bad TP=0.0 SL=0.0 — skip"). Use Decimal to
    # get an exact decimal exponent regardless of how Python's repr renders the float.
    exponent = Decimal(str(tick_size)).normalize().as_tuple().exponent
    prec = max(-exponent, 0) if isinstance(exponent, int) else 0
    return round(round(price / tick_size) * tick_size, prec)


def calc_qty(price, qty_usdt, qty_step, min_qty):
    raw     = qty_usdt / price
    stepped = math.floor(raw / qty_step) * qty_step
    result  = max(stepped, min_qty)
    # Same scientific-notation fix as round_price() — qty_step can also arrive
    # as e.g. 1e-06 for some pairs.
    exponent = Decimal(str(qty_step)).normalize().as_tuple().exponent
    prec = max(-exponent, 0) if isinstance(exponent, int) else 0
    return str(round(result, prec))


def calc_adaptive_qty(price, atr_pct, qty_step, min_qty):
    """Размер позиции в монетах. atr_pct — ATR в ПРОЦЕНТАХ от цены (0.27 == 0.27%),
    ровно как возвращает detect_market_mode().

    FIX 2026-10-18: раньше atr_pct (в %) подставлялся в формулу как доля, из-за чего
    vol_mult был ~0.27 вместо ~0.97 и размер ВСЕГДА упирался в MIN_POSITION_USDT
    (QTY_USDT фактически игнорировался).
    """
    if not atr_pct or atr_pct <= 0:
        atr_pct = 2.0
    atr_frac = atr_pct / 100.0
    vol_mult = 1 / (1 + atr_frac * 10)
    qty_usdt = max(min(QTY_USDT_BASE * vol_mult, MAX_POSITION_USDT), MIN_POSITION_USDT)
    return calc_qty(price, qty_usdt, qty_step, min_qty)


def get_position(symbol):
    resp = session.get_positions(category="linear", symbol=symbol)
    for p in resp["result"]["list"]:
        if float(p["size"]) > 0:
            return p
    return None


def get_last_closed_pnl(symbol):
    """
    Fetch the most recent exchange-side closed PnL record for symbol.
    Used to reconcile trades that were closed by Bybit itself (TP/SL hit
    server-side while the bot was sleeping between ticks) — these never
    go through our close_position() and were previously invisible to
    trades.json / /weekly / /status.
    """
    try:
        resp = session.get_closed_pnl(category="linear", symbol=symbol, limit=1)
        rows = resp.get("result", {}).get("list", [])
        if not rows:
            return None
        row = rows[0]
        return {
            "side":        row.get("side"),            # side of the CLOSING order
            "entry_price": float(row.get("avgEntryPrice", 0) or 0),
            "exit_price":  float(row.get("avgExitPrice", 0) or 0),
            "pnl":         float(row.get("closedPnl", 0) or 0),
            "updated_time": int(row.get("updatedTime", 0) or 0),
        }
    except Exception as e:
        log.warning(f"[{symbol}] get_last_closed_pnl error: {e}")
        return None


def cancel_orders(symbol):
    try:
        session.cancel_all_orders(category="linear", symbol=symbol)
    except Exception:
        pass


def set_leverage_safe(symbol):
    try:
        session.set_leverage(
            category="linear", symbol=symbol,
            buyLeverage=str(LEVERAGE), sellLeverage=str(LEVERAGE),
        )
    except Exception:
        pass


def check_funding_rate(symbol) -> bool:
    """Return False if funding rate too high (>0.05%)."""
    try:
        resp = session.get_tickers(category="linear", symbol=symbol)
        item = resp["result"]["list"][0]
        # FIX: key varies across pybit versions
        rate_str = item.get("fundingRate") or item.get("lastFundingRate") or "0"
        if abs(float(rate_str)) > 0.0005:
            log.warning(f"[{symbol}] High funding {float(rate_str):.4%} — skip entry")
            return False
    except Exception as e:
        log.debug(f"[{symbol}] Funding check error: {e}")
    return True


def open_position(symbol, side, price, atr, tick_size, qty_step, min_qty, mode, atr_pct=None, adx=None):
    # 1. AI veto
    if HERMES_ENABLED and brain:
        try:
            # FIX: correct parameter names — signal="BUY"/"SELL", not side="long"/"short"
            trade_ok, reason = brain.should_trade(
                symbol=symbol,
                signal="BUY" if side == "Buy" else "SELL",
                price=price,
                rsi=coin_data.get(symbol, {}).get("rsi", 50),
                # FIX 2026-10-15: was hardcoded 25.0 — now the real computed ADX
                # (falls back to the trend threshold if ADX couldn't be computed
                # yet, e.g. not enough candle history).
                adx=adx if adx is not None else ADX_TREND_THRESHOLD,
                atr_pct=atr_pct if atr_pct else 2.0,
                mode=mode,
                vol_ratio=1.0,
                include_news=False,  # FIX: Hermes has no internet access via OpenRouter
            )
            if not trade_ok:
                log.warning(f"[{symbol}] AI VETO: {reason}")
                return
            log.info(f"[{symbol}] AI approved: {reason}")
        except Exception as e:
            log.error(f"[{symbol}] AI check error: {e}")

    # 2. Funding rate check
    if not check_funding_rate(symbol):
        return

    # 3. Adaptive qty
    qty = calc_adaptive_qty(price, atr_pct, qty_step, min_qty)

    # 4. TP/SL with zero-protection
    min_move = max(price * MIN_SL_PCT, tick_size * 10)
    tp_move  = max(atr * ATR_TP_MULT, min_move * 2) if atr and atr > 0 else price * 0.03
    sl_move  = max(atr * ATR_SL_MULT, min_move)     if atr and atr > 0 else price * 0.015

    if side == "Buy":
        tp = round_price(price + tp_move, tick_size)
        sl = round_price(price - sl_move, tick_size)
    else:
        tp = round_price(price - tp_move, tick_size)
        sl = round_price(price + sl_move, tick_size)

    if tp <= 0 or sl <= 0 or tp == price or sl == price:
        log.warning(f"[{symbol}] Bad TP={tp} SL={sl} — skip")
        return

    # 5. Limit IOC with Market fallback
    limit_px = round_price(price * (1.003 if side == "Buy" else 0.997), tick_size)
    rr = tp_move / sl_move if sl_move else 0.0
    log.info(f"[{symbol}] {'LONG' if side=='Buy' else 'SHORT'} Limit IOC "
             f"qty:{qty}(~{float(qty)*price:.1f}$) entry:{limit_px} TP:{tp} SL:{sl} "
             f"R:R={rr:.2f} {mode}")

    session.place_order(
        category="linear", symbol=symbol,
        side=side, orderType="Limit", qty=qty,
        price=str(limit_px), timeInForce="IOC",
        takeProfit=str(tp), stopLoss=str(sl),
        tpTriggerBy="MarkPrice", slTriggerBy="MarkPrice",
    )

    # FIX: check if IOC filled, fallback to Market if not
    time.sleep(0.5)
    if get_position(symbol) is None:
        log.warning(f"[{symbol}] IOC didn't fill — trying Market order")
        session.place_order(
            category="linear", symbol=symbol,
            side=side, orderType="Market", qty=qty,
            takeProfit=str(tp), stopLoss=str(sl),
            tpTriggerBy="MarkPrice", slTriggerBy="MarkPrice",
        )

    label = "LONG" if side == "Buy" else "SHORT"
    log.info(f"[{symbol}] ✅ {label} | TP:{tp} SL:{sl}")
    tg.notify_position_opened(symbol, side, qty, price, tp, sl)
    # FIX: use setdefault to prevent KeyError if coin_data[symbol] missing
    coin_data.setdefault(symbol, {}).update({
        "open_price": price,
        "open_side":  side,
        "open_mode":  mode,
        "open_time":  time.time(),
    })


def close_position(symbol, side, qty, pnl=0.0, result="signal", exit_price=None):
    global consecutive_stops, block_new_trades
    # FIX: guard against qty=None (when position wasn't open)
    if not qty:
        log.warning(f"[{symbol}] close_position called with qty=None — skip")
        return
    cancel_orders(symbol)
    close_side = "Sell" if side == "Buy" else "Buy"
    session.place_order(
        category="linear", symbol=symbol,
        side=close_side, orderType="Market",
        qty=qty, reduceOnly=True,
    )
    log.info(f"[{symbol}] Closed | PnL:{pnl:+.4f}")
    tg.notify_position_closed(symbol, side, pnl)
    consecutive_stops = consecutive_stops + 1 if pnl < 0 else 0
    # FIX 2026-10-15: start a cooldown on this symbol after any losing close,
    # so bot_thread() won't immediately re-enter the same adverse move.
    if pnl < 0:
        last_stop_time[symbol] = time.time()
    # FIX 2026-10-15: circuit breaker used to only react once every
    # SCAN_INTERVAL_MIN (up to 30min of extra damage). Now block_new_trades
    # flips immediately, right here, the moment the 3rd consecutive stop
    # closes — main()'s periodic adapt() loop still resets it afterwards.
    if consecutive_stops >= 3 and not block_new_trades:
        block_new_trades = True
        log.warning(f"[{symbol}] ⚠️ {consecutive_stops} consecutive stops → blocking new entries NOW")
        tg.send_message(f"⚠️ <b>Серия стопов ({consecutive_stops})</b>\nНовые входы заблокированы немедленно.")
    tracker.record_trade(
        symbol=symbol, side=side, result=result, pnl=pnl,
        entry_price=coin_data.get(symbol, {}).get("open_price"),
        exit_price=exit_price or coin_data.get(symbol, {}).get("price"),
        mode=coin_data.get(symbol, {}).get("open_mode"),
    )


# ═══════════════════════════════════════════════════
#   АДАПТАЦИЯ RSI
# ═══════════════════════════════════════════════════

def update_env_rsi(new_buy, new_sell):
    try:
        with open(".env", "r") as f:
            lines = f.readlines()
        new_lines, fb, fs = [], False, False
        for line in lines:
            if   line.startswith("RSI_BUY="):  new_lines.append(f"RSI_BUY={new_buy}\n");   fb = True
            elif line.startswith("RSI_SELL="): new_lines.append(f"RSI_SELL={new_sell}\n"); fs = True
            else:                               new_lines.append(line)
        if not fb: new_lines.append(f"RSI_BUY={new_buy}\n")
        if not fs: new_lines.append(f"RSI_SELL={new_sell}\n")
        with open(".env", "w") as f:
            f.writelines(new_lines)
    except Exception as e:
        log.warning(f"Could not update .env: {e}")


def adapt_rsi_thresholds(coins):
    global RSI_BUY_CURRENT, RSI_SELL_CURRENT
    # v3.2: RSI_BUY/RSI_SELL читает только трендовая логика (отключена), а эта
    # функция при каждом скане переписывала .env и затирала ручные значения.
    if not ADAPT_RSI:
        return
    rsi_vals = [c["rsi"] for c in coins if c.get("rsi") is not None]
    if not rsi_vals:
        return
    avg     = round(sum(rsi_vals) / len(rsi_vals), 1)
    ob, os_ = RSI_BUY_CURRENT, RSI_SELL_CURRENT
    if   avg < 40: nb = round(min(avg + 15, 50), 1); ns = round(max(nb + 15, 55), 1)
    elif avg > 60: ns = round(max(avg - 15, 50), 1); nb = round(min(ns - 15, 50), 1)
    else:          nb, ns = 42.0, 58.0
    nb = round(max(30, min(nb, 50)), 1)
    ns = round(max(50, min(ns, 70)), 1)
    if nb != ob or ns != os_:
        RSI_BUY_CURRENT, RSI_SELL_CURRENT = nb, ns
        update_env_rsi(nb, ns)
        log.info(f"RSI: BUY {ob}→{nb}, SELL {os_}→{ns} (avg={avg})")
        tg.send_message(
            f"🔧 <b>RSI адаптированы</b>\nAvg RSI рынка: {avg}\n"
            f"BUY: {ob}→<b>{nb}</b>  SELL: {os_}→<b>{ns}</b>"
        )


# ═══════════════════════════════════════════════════
#   СТАТУС
# ═══════════════════════════════════════════════════

def save_status():
    alive = [s for s, t in active_bots.items() if t.is_alive()]
    data  = {
        "active_bots": alive, "max_bots": MAX_BOTS,
        "daily_pnl": daily_pnl, "daily_loss_limit": DAILY_LOSS_LIMIT,
        "coins": coin_data, "rsi_buy": RSI_BUY_CURRENT, "rsi_sell": RSI_SELL_CURRENT,
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    try:
        with open(STATUS_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        log.warning(f"save_status error: {e}")


# ═══════════════════════════════════════════════════
#   ПОТОК БОТА
# ═══════════════════════════════════════════════════

def bot_thread(symbol: str, stop_event: threading.Event):
    global daily_pnl, consecutive_stops, block_new_trades
    log.info(f"[{symbol}] ▶ started")
    set_leverage_safe(symbol)
    try:
        tick_size, qty_step, min_qty = get_instrument_info(symbol)
        log.info(f"[{symbol}] tick:{tick_size} step:{qty_step} min:{min_qty}")
    except Exception as e:
        log.error(f"[{symbol}] get_instrument_info failed: {e}")
        return

    # FIX 2026-10-13: recover "we have an open position" memory after a manager
    # restart. coin_data is purely in-memory — if the bot restarts (deploy,
    # crash, systemd bounce) while a position is open on the exchange,
    # open_price/open_side are lost. Without this, the reconciliation logic
    # in the loop below (which needs open_price to detect server-side TP/SL
    # fills) would never trigger for that position, and its eventual close
    # would again be invisible to trades.json — same symptom as the original
    # bug, just triggered by a restart instead of a sleep-through-fill.
    existing_pos = get_position(symbol)
    if existing_pos:
        entry_px = float(existing_pos.get("avgPrice", 0) or 0)
        coin_data.setdefault(symbol, {}).update({
            "open_price": entry_px or None,
            "open_side":  existing_pos.get("side"),
            "open_mode":  "unknown",  # mode wasn't persisted across restart
            "open_time":  time.time(),
        })
        log.info(
            f"[{symbol}] 🔄 Recovered open position after restart: "
            f"{existing_pos.get('side')} entry:{entry_px}"
        )

    while not stop_event.is_set():
        try:
            closes, highs, lows, volumes = get_klines(symbol, INTERVAL)
            price   = closes[-1]
            rsi     = calc_rsi(closes, RSI_PERIOD)
            atr     = calc_atr(closes, highs, lows, ATR_PERIOD)
            mode, _, atr_pct, adx = detect_market_mode(closes, highs, lows, price)
            pos      = get_position(symbol)
            pos_side = pos["side"] if pos else None
            pos_qty  = pos["size"] if pos else None  # FIX: None not "0"
            pnl      = float(pos["unrealisedPnl"]) if pos else 0.0

            # FIX 2026-10-13: reconcile server-side TP/SL fills. If we believed
            # a position was open (coin_data has open_price from open_position())
            # but the exchange now reports no position, Bybit closed it itself
            # (TP or SL triggered) while this thread was asleep between ticks.
            # Without this, such trades were NEVER recorded — no trades.json
            # entry, no /weekly data, no Telegram notification — even though
            # real money moved on the exchange.
            prev_open_price = coin_data.get(symbol, {}).get("open_price")
            prev_open_side  = coin_data.get(symbol, {}).get("open_side")
            if pos is None and prev_open_price is not None:
                closed = get_last_closed_pnl(symbol)
                if closed and closed["updated_time"] > last_recorded_close_ts.get(symbol, 0):
                    last_recorded_close_ts[symbol] = closed["updated_time"]
                    result_label = "TP" if closed["pnl"] > 0 else ("SL" if closed["pnl"] < 0 else "signal")
                    log.info(
                        f"[{symbol}] 🔁 Exchange closed position server-side "
                        f"({result_label}) entry:{closed['entry_price']} "
                        f"exit:{closed['exit_price']} PnL:{closed['pnl']:+.4f}"
                    )
                    tg.notify_position_closed(symbol, prev_open_side or closed["side"], closed["pnl"])
                    tracker.record_trade(
                        symbol=symbol, side=prev_open_side or closed["side"],
                        result=result_label, pnl=closed["pnl"],
                        entry_price=closed["entry_price"] or prev_open_price,
                        exit_price=closed["exit_price"],
                        mode=coin_data.get(symbol, {}).get("open_mode") or "unknown",
                    )
                    daily_pnl += closed["pnl"]
                    consecutive_stops = consecutive_stops + 1 if closed["pnl"] < 0 else 0
                    # FIX 2026-10-15: server-side TP/SL fills need the same
                    # cooldown bookkeeping as bot-initiated close_position().
                    if closed["pnl"] < 0:
                        last_stop_time[symbol] = time.time()
                    if consecutive_stops >= 3 and not block_new_trades:
                        block_new_trades = True
                        log.warning(f"[{symbol}] ⚠️ {consecutive_stops} consecutive stops (server-side) → blocking new entries NOW")
                        tg.send_message(f"⚠️ <b>Серия стопов ({consecutive_stops})</b>\nНовые входы заблокированы немедленно.")
                elif closed is None:
                    log.warning(f"[{symbol}] Position vanished but no closed-PnL record found — exchange API lag?")
                # Either way, clear the "we have an open position" memory so we
                # don't keep re-checking / re-notifying on every tick.
                coin_data.setdefault(symbol, {}).update({
                    "open_price": None, "open_side": None, "open_mode": None,
                })

            log.info(
                f"[{symbol}] {price:.6f} RSI:{rsi} ATR%:{atr_pct} "
                f"{mode} pos:{pos_side or 'none'} PnL:{pnl:+.4f}"
            )

            # FIX: setdefault preserves open_price/side/mode across iterations
            coin_data.setdefault(symbol, {}).update({
                "price": price, "rsi": rsi, "atr_pct": atr_pct,
                "mode": mode, "position": pos_side or "нет", "pnl": pnl,
            })
            save_status()

            if daily_pnl <= DAILY_LOSS_LIMIT:
                log.warning(f"[{symbol}] Daily loss limit hit!")
                tg.notify_daily_limit(symbol)
                if pos_side and pos_qty:
                    close_position(symbol, pos_side, pos_qty, pnl)
                break

            signal = None
            # FIX 2026-10-15: cooldown gate — after a stop-loss on this symbol,
            # don't open a new position here for STOP_COOLDOWN_MIN minutes.
            cooldown_left = 0.0
            last_stop = last_stop_time.get(symbol)
            if last_stop is not None:
                cooldown_left = STOP_COOLDOWN_MIN * 60 - (time.time() - last_stop)
            in_cooldown = cooldown_left > 0
            if block_new_trades and not pos_side:
                log.info(f"[{symbol}] New entries blocked")
            elif in_cooldown and not pos_side:
                log.info(f"[{symbol}] Cooldown after stop — {cooldown_left/60:.1f}min left, skip new entries")
            elif mode == "sideways":
                signal, _, _ = signal_sideways(closes, highs, lows, price, rsi, volumes)
                if signal and pos_side and pos_side != signal:
                    if pnl > 0:
                        daily_pnl += pnl
                        close_position(symbol, pos_side, pos_qty, pnl)
                        time.sleep(1); pos_side, pos_qty = None, None
                    else:
                        log.info(f"[{symbol}] Opposite signal but PnL{pnl:+.4f} — hold for TP/SL")
                if signal and pos_side is None:
                    open_position(symbol, signal, price, atr, tick_size, qty_step, min_qty, mode, atr_pct, adx)
            else:
                # FIX 2026-10-18: Не торгуем в режиме тренда — высокий риск SL.
                # Бот заточен под боковик (RSI+BB), трендовые сигналы отключены.
                # v3.2: adx может быть None (мало свечей / ATR не посчитан) —
                # форматирование f"{adx:.1f}" на None падало с TypeError.
                adx_txt = f"{adx:.1f}" if adx is not None else "n/a"
                log.info(f"[{symbol}] Trending mode (ADX:{adx_txt}) — skip entry")
                signal = None

            if not signal:
                log.info(f"[{symbol}] No signal, waiting...")

        except Exception as e:
            log.error(f"[{symbol}] Error: {e}")

        stop_event.wait(timeout=SLEEP_SEC)

    log.info(f"[{symbol}] ⏹ stopped")


# ═══════════════════════════════════════════════════
#   МЕНЕДЖЕР БОТОВ
# ═══════════════════════════════════════════════════

def start_bot(symbol: str):
    if symbol in active_bots and active_bots[symbol].is_alive():
        return
    ev = threading.Event()
    th = threading.Thread(target=bot_thread, args=(symbol, ev),
                          name=f"bot-{symbol}", daemon=True)
    stop_flags[symbol]  = ev
    active_bots[symbol] = th
    th.start()
    log.info(f"✅ started: {symbol}")
    tg.notify_bot_started(symbol)


def stop_bot(symbol: str):
    if symbol in stop_flags:
        stop_flags[symbol].set()
    if symbol in active_bots:
        active_bots[symbol].join(timeout=5)
        del active_bots[symbol]; del stop_flags[symbol]
    coin_data.pop(symbol, None)
    log.info(f"🛑 stopped: {symbol}")
    tg.notify_bot_stopped(symbol)


def stop_all_bots():
    for s in list(active_bots.keys()):
        stop_bot(s)


def print_status():
    alive = [s for s, t in active_bots.items() if t.is_alive()]
    modes = {s: coin_data.get(s, {}).get("mode", "?") for s in alive}
    print(f"\n{'─'*54}")
    print(f"  📊 Bots ({len(alive)}/{MAX_BOTS}): "
          f"{', '.join(f'{s}({m})' for s,m in modes.items()) or 'none'}")
    print(f"  💸 PnL:{daily_pnl:+.4f}$ | RSI:<{RSI_BUY_CURRENT}/{RSI_SELL_CURRENT}")
    print(f"{'─'*54}")


# ═══════════════════════════════════════════════════
#   ГЛАВНЫЙ ЦИКЛ
# ═══════════════════════════════════════════════════

def main():
    # v3.2: все global — в начале функции (mid-function global = SyntaxError,
    # если имя уже использовалось выше)
    global block_new_trades, adapter_counter, consecutive_stops, daily_pnl
    global RSI_BUY_CURRENT, RSI_SELL_CURRENT

    log.info("=" * 54)
    log.info(f"  🚀 MANAGER v3.2 | Hermes:{'ON' if HERMES_ENABLED else 'OFF'}")
    log.info(f"  BB{BB_PERIOD}/{BB_STD}σ + RSI + ATR{ATR_PERIOD} | TP×{ATR_TP_MULT} SL×{ATR_SL_MULT}")
    log.info(f"  MaxBots:{MAX_BOTS} | Limit:{DAILY_LOSS_LIMIT}$ | QTY:{QTY_USDT_BASE}$ | MinSL:{MIN_SL_PCT:.2%}")
    log.info("=" * 54)
    tg.notify_manager_started(MAX_BOTS, SCAN_INTERVAL_MIN)

    last_reset_day = datetime.now().strftime("%Y-%m-%d")

    try:
        while True:
            today = datetime.now().strftime("%Y-%m-%d")
            if today != last_reset_day:
                daily_pnl = 0.0; block_new_trades = False; consecutive_stops = 0
                last_reset_day = today
                log.info("🌅 New day — PnL reset")
                tg.send_message("🌅 <b>Новый день</b>\nPnL сброшен.")

            # FIX: pass shared session to scanner to reuse connection
            coins       = scan(session=session, top_n=MAX_BOTS)
            print_scan_results(coins)
            new_symbols = {c["symbol"] for c in coins}

            adapt_rsi_thresholds(coins)

            # AI Strategy Manager (2x/day)
            if HERMES_ENABLED and strategy_manager:
                ctx = {
                    "daily_pnl":      daily_pnl,
                    "active_bots":    len([t for t in active_bots.values() if t.is_alive()]),
                    "max_bots":       MAX_BOTS,
                    "recent_coins":   [c["symbol"] for c in coins],
                    "avg_volatility": (sum(c.get("atr_pct", 2) for c in coins) / len(coins) if coins else 2),
                    "market_trend":   "volatile" if any(c.get("atr_pct", 0) > 5 for c in coins) else "stable",
                }
                cur = {"RSI_BUY": RSI_BUY_CURRENT, "RSI_SELL": RSI_SELL_CURRENT,
                       "QTY_USDT": QTY_USDT_BASE,  "MAX_BOTS": MAX_BOTS,
                       "SCAN_INTERVAL_MIN": SCAN_INTERVAL_MIN}
                try:
                    adj, new_p, reason = strategy_manager.adjust_strategy(cur, ctx)
                    if adj:
                        RSI_BUY_CURRENT  = new_p.get("RSI_BUY",  RSI_BUY_CURRENT)
                        RSI_SELL_CURRENT = new_p.get("RSI_SELL", RSI_SELL_CURRENT)
                        reload_config()  # FIX: hot-reload after AI changes .env
                        tg.send_message(
                            f"🧠 <b>AI скорректировал стратегию</b>\n{reason}\n"
                            f"RSI: {RSI_BUY_CURRENT}/{RSI_SELL_CURRENT}\n"
                            f"QTY: {new_p.get('QTY_USDT', QTY_USDT_BASE)} USDT"
                        )
                except Exception as e:
                    log.error(f"AI strategy error: {e}")

            adapter_counter += 1
            if consecutive_stops >= 3 or adapter_counter % 3 == 0:
                if consecutive_stops >= 3:
                    log.warning(f"⚠️ {consecutive_stops} consecutive stops → emergency adapt")
                    tg.send_message(f"⚠️ <b>Серия стопов</b>\n{consecutive_stops} подряд")
                result = adapter.adapt(daily_pnl, RSI_BUY_CURRENT, RSI_SELL_CURRENT)
                block_new_trades = result["block_new_trades"]
                RSI_BUY_CURRENT  = result["rsi_buy"]
                RSI_SELL_CURRENT = result["rsi_sell"]
                adapter.notify_if_changed(result)
                if consecutive_stops >= 3:
                    consecutive_stops = 0

            for symbol in list(active_bots.keys()):
                if symbol not in new_symbols:
                    log.info(f"📤 {symbol} out of top")
                    stop_bot(symbol)

            for symbol in new_symbols:
                if len([t for t in active_bots.values() if t.is_alive()]) >= MAX_BOTS:
                    break
                start_bot(symbol)

            print_status()
            save_status()
            log.info(f"⏳ Next scan in {SCAN_INTERVAL_MIN} min...")
            time.sleep(SCAN_INTERVAL_MIN * 60)

    except KeyboardInterrupt:
        log.info("⛔ Stopping...")
    finally:
        stop_all_bots()
        tg.notify_manager_stopped()
        log.info("✅ Stopped.")


if __name__ == "__main__":
    main()
