"""
╔══════════════════════════════════════════════════════╗
║           MANAGER.PY — Адаптивный менеджер v3        ║
║                                                      ║
║  Fixes v3:                                           ║
║  • load_dotenv() BEFORE imports (HERMES_ENABLED fix) ║
║  • reload_config() — hot reload without restart     ║
║  • pos_qty None guard in close_position             ║
║  • funding rate key fix (fundingRate/lastFundingRate)║
║  • IOC order fill check with Market fallback        ║
║  • open_position signal="BUY"/"SELL" (not side)    ║
║  • coin_data.setdefault() prevents KeyError         ║
║  • scan() passes shared session                     ║
╚══════════════════════════════════════════════════════╝
"""

import os
import json
import math
import time
import threading
import logging
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
DAILY_LOSS_LIMIT  = float(os.getenv("DAILY_LOSS_LIMIT",      "-50"))
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

RSI_BUY_CURRENT  = float(os.getenv("RSI_BUY",  "42"))
RSI_SELL_CURRENT = float(os.getenv("RSI_SELL", "58"))

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


def detect_market_mode(closes, highs, lows, price):
    atr = calc_atr(closes, highs, lows, ATR_PERIOD)
    if atr is None:
        return "trend", None, None
    atr_pct = atr / price
    ema20   = calc_ema(closes, 20)
    ema50   = calc_ema(closes, 50)
    if ema20 and ema50:
        ema_diff = abs(ema20 - ema50) / price
        mode = "sideways" if (atr_pct < SIDEWAYS_ATR_THRESHOLD and ema_diff < 0.02) else "trend"
    else:
        mode = "sideways" if atr_pct < SIDEWAYS_ATR_THRESHOLD else "trend"
    return mode, atr, round(atr_pct * 100, 2)


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
    if prev <= lower and price > lower and rsi < 55 and vol_ok:
        return "Buy",  mid, lower
    if prev >= upper and price < upper and rsi > 45 and vol_ok:
        return "Sell", mid, upper
    return None, None, None


def signal_trend(rsi):
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
    prec = len(str(tick_size).rstrip("0").split(".")[-1]) if "." in str(tick_size) else 0
    return round(round(price / tick_size) * tick_size, prec)


def calc_qty(price, qty_usdt, qty_step, min_qty):
    raw     = qty_usdt / price
    stepped = math.floor(raw / qty_step) * qty_step
    result  = max(stepped, min_qty)
    prec    = len(str(qty_step).rstrip("0").split(".")[-1]) if "." in str(qty_step) else 0
    return str(round(result, prec))


def calc_adaptive_qty(price, atr_pct, qty_step, min_qty):
    if not atr_pct or atr_pct <= 0:
        atr_pct = 0.02
    vol_mult = 1 / (1 + atr_pct * 10)
    qty_usdt = max(min(QTY_USDT_BASE * vol_mult, MAX_POSITION_USDT), MIN_POSITION_USDT)
    return calc_qty(price, qty_usdt, qty_step, min_qty)


def get_position(symbol):
    resp = session.get_positions(category="linear", symbol=symbol)
    for p in resp["result"]["list"]:
        if float(p["size"]) > 0:
            return p
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


def open_position(symbol, side, price, atr, tick_size, qty_step, min_qty, mode, atr_pct=None):
    # 1. AI veto
    if HERMES_ENABLED and brain:
        try:
            # FIX: correct parameter names — signal="BUY"/"SELL", not side="long"/"short"
            trade_ok, reason = brain.should_trade(
                symbol=symbol,
                signal="BUY" if side == "Buy" else "SELL",
                price=price,
                rsi=coin_data.get(symbol, {}).get("rsi", 50),
                adx=25.0,
                atr_pct=atr_pct or 0.02,
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
    qty = calc_adaptive_qty(price, atr_pct or 0.02, qty_step, min_qty)

    # 4. TP/SL with zero-protection
    min_move = max(price * 0.005, tick_size * 10)
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
    log.info(f"[{symbol}] {'LONG' if side=='Buy' else 'SHORT'} Limit IOC "
             f"qty:{qty}(~{float(qty)*price:.1f}$) entry:{limit_px} TP:{tp} SL:{sl} {mode}")

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
    global consecutive_stops
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
    log.info(f"[{symbol}] ▶ started")
    set_leverage_safe(symbol)
    try:
        tick_size, qty_step, min_qty = get_instrument_info(symbol)
        log.info(f"[{symbol}] tick:{tick_size} step:{qty_step} min:{min_qty}")
    except Exception as e:
        log.error(f"[{symbol}] get_instrument_info failed: {e}")
        return

    while not stop_event.is_set():
        try:
            closes, highs, lows, volumes = get_klines(symbol, INTERVAL)
            price   = closes[-1]
            rsi     = calc_rsi(closes, RSI_PERIOD)
            atr     = calc_atr(closes, highs, lows, ATR_PERIOD)
            mode, _, atr_pct = detect_market_mode(closes, highs, lows, price)
            pos      = get_position(symbol)
            pos_side = pos["side"] if pos else None
            pos_qty  = pos["size"] if pos else None  # FIX: None not "0"
            pnl      = float(pos["unrealisedPnl"]) if pos else 0.0

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
            if block_new_trades and not pos_side:
                log.info(f"[{symbol}] New entries blocked")
            elif mode == "sideways":
                signal, _, _ = signal_sideways(closes, highs, lows, price, rsi, volumes)
                if signal and pos_side and pos_side != signal:
                    if pnl > 0:
                        global daily_pnl
                        daily_pnl += pnl
                        close_position(symbol, pos_side, pos_qty, pnl)
                        time.sleep(1); pos_side, pos_qty = None, None
                    else:
                        log.info(f"[{symbol}] Opposite signal but PnL{pnl:+.4f} — hold for TP/SL")
                if signal and pos_side is None:
                    open_position(symbol, signal, price, atr, tick_size, qty_step, min_qty, mode, atr_pct)
            else:
                signal = signal_trend(rsi)
                if signal and pos_side and pos_side != signal:
                    if pnl > 0:
                        daily_pnl += pnl
                        close_position(symbol, pos_side, pos_qty, pnl)
                        time.sleep(1); pos_side, pos_qty = None, None
                    else:
                        log.info(f"[{symbol}] Opposite signal but PnL{pnl:+.4f} — hold for TP/SL")
                if signal and pos_side is None:
                    open_position(symbol, signal, price, atr, tick_size, qty_step, min_qty, mode, atr_pct)

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
    global block_new_trades, adapter_counter, consecutive_stops

    log.info("=" * 54)
    log.info(f"  🚀 MANAGER v3 | Hermes:{'ON' if HERMES_ENABLED else 'OFF'}")
    log.info(f"  BB{BB_PERIOD}/{BB_STD}σ + RSI + ATR{ATR_PERIOD} | TP×{ATR_TP_MULT} SL×{ATR_SL_MULT}")
    log.info(f"  MaxBots:{MAX_BOTS} | Limit:{DAILY_LOSS_LIMIT}$")
    log.info("=" * 54)
    tg.notify_manager_started(MAX_BOTS, SCAN_INTERVAL_MIN)

    last_reset_day = datetime.now().strftime("%Y-%m-%d")

    try:
        while True:
            today = datetime.now().strftime("%Y-%m-%d")
            if today != last_reset_day:
                global daily_pnl
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
