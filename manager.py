"""
╔══════════════════════════════════════════════════════╗
║           MANAGER.PY — Адаптивный менеджер          ║
║                                                      ║
║  Стратегия:                                          ║
║  • Определяет режим рынка (боковик / тренд)         ║
║  • БОКОВИК → Bollinger Bands + RSI-фильтр           ║
║  • ТРЕНД   → RSI-стратегия (как раньше)             ║
║  • TP/SL   → адаптивный, на основе ATR              ║
║  • Пороги  → автоматически подстраиваются           ║
╚══════════════════════════════════════════════════════╝
"""

import os
import json
import time
import threading
import logging
from datetime import datetime
from dotenv import load_dotenv
from pybit.unified_trading import HTTP

from scanner import scan, print_scan_results
import telegram_notifier as tg
import pnl_tracker as tracker
import analytics
import strategy_adapter as adapter

load_dotenv()

# ── Настройки ────────────────────────────────────────
SCAN_INTERVAL_MIN = int(os.getenv("SCAN_INTERVAL_MIN", "30"))
MAX_BOTS          = int(os.getenv("MAX_BOTS",          "3"))
DAILY_LOSS_LIMIT  = float(os.getenv("DAILY_LOSS_LIMIT","-20"))
LEVERAGE          = int(os.getenv("LEVERAGE",          "3"))
QTY               = os.getenv("QTY",       "10")
INTERVAL          = os.getenv("INTERVAL",  "15")
RSI_PERIOD        = int(os.getenv("RSI_PERIOD", "14"))
SLEEP_SEC         = int(os.getenv("SLEEP_SEC",  "60"))

# Bollinger Bands
BB_PERIOD   = int(os.getenv("BB_PERIOD",  "20"))
BB_STD      = float(os.getenv("BB_STD",   "2.0"))

# ATR
ATR_PERIOD  = int(os.getenv("ATR_PERIOD", "14"))
ATR_TP_MULT = float(os.getenv("ATR_TP_MULT", "2.0"))  # TP = ATR × 2
ATR_SL_MULT = float(os.getenv("ATR_SL_MULT", "1.0"))  # SL = ATR × 1

# Режим рынка
SIDEWAYS_ATR_THRESHOLD = float(os.getenv("SIDEWAYS_ATR_THRESHOLD", "0.03"))  # ATR/цена < 3% = боковик

# RSI пороги (адаптируются автоматически)
RSI_BUY_CURRENT  = float(os.getenv("RSI_BUY",  "45"))
RSI_SELL_CURRENT = float(os.getenv("RSI_SELL", "55"))

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
active_bots: dict[str, threading.Thread] = {}
stop_flags:  dict[str, threading.Event]  = {}
daily_pnl:   float = 0.0
coin_data:   dict[str, dict] = {}
block_new_trades: bool = False    # блокировка новых входов при просадке
adapter_counter:  int  = 0        # счётчик для запуска адаптации раз в N сканирований
STATUS_FILE = "logs/status.json"


# ═══════════════════════════════════════════════════
#   ТЕХНИЧЕСКИЕ ИНДИКАТОРЫ
# ═══════════════════════════════════════════════════

def get_klines(symbol, interval, limit=150):
    resp = session.get_kline(
        category="linear", symbol=symbol,
        interval=interval, limit=limit,
    )
    candles = resp["result"]["list"]
    candles = list(reversed(candles))
    closes = [float(c[4]) for c in candles]
    highs  = [float(c[2]) for c in candles]
    lows   = [float(c[3]) for c in candles]
    return closes, highs, lows


def calc_rsi(closes, period=14):
    gains, losses = [], []
    for i in range(1, len(closes)):
        diff = closes[i] - closes[i - 1]
        gains.append(max(diff, 0))
        losses.append(max(-diff, 0))
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
    if avg_loss == 0:
        return 100.0
    return round(100 - (100 / (1 + avg_gain / avg_loss)), 2)


def calc_bollinger(closes, period=20, std_mult=2.0):
    """Возвращает (middle, upper, lower) последней свечи"""
    if len(closes) < period:
        return None, None, None
    window = closes[-period:]
    mid = sum(window) / period
    variance = sum((x - mid) ** 2 for x in window) / period
    std = variance ** 0.5
    return round(mid, 8), round(mid + std_mult * std, 8), round(mid - std_mult * std, 8)


def calc_atr(closes, highs, lows, period=14):
    """Average True Range"""
    trs = []
    for i in range(1, len(closes)):
        tr = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i]  - closes[i - 1]),
        )
        trs.append(tr)
    if len(trs) < period:
        return None
    atr = sum(trs[:period]) / period
    for tr in trs[period:]:
        atr = (atr * (period - 1) + tr) / period
    return round(atr, 8)


def calc_ema(closes, period=20):
    if len(closes) < period:
        return None
    ema = sum(closes[:period]) / period
    k = 2 / (period + 1)
    for price in closes[period:]:
        ema = price * k + ema * (1 - k)
    return round(ema, 8)


def detect_market_mode(closes, highs, lows, price):
    """
    Определяет режим рынка:
      'sideways' — боковик (ATR/цена мала, EMA50 и EMA20 рядом)
      'trend'    — тренд

    Возвращает (mode, atr, atr_pct)
    """
    atr = calc_atr(closes, highs, lows, ATR_PERIOD)
    if atr is None:
        return "trend", None, None

    atr_pct = atr / price  # относительный ATR

    ema20 = calc_ema(closes, 20)
    ema50 = calc_ema(closes, 50)

    if ema20 is None or ema50 is None:
        mode = "sideways" if atr_pct < SIDEWAYS_ATR_THRESHOLD else "trend"
        return mode, atr, round(atr_pct * 100, 2)

    ema_diff_pct = abs(ema20 - ema50) / price

    # Боковик: ATR небольшой И EMA20 и EMA50 близко друг к другу
    if atr_pct < SIDEWAYS_ATR_THRESHOLD and ema_diff_pct < 0.02:
        mode = "sideways"
    else:
        mode = "trend"

    return mode, atr, round(atr_pct * 100, 2)


# ═══════════════════════════════════════════════════
#   ТОРГОВЫЕ СИГНАЛЫ
# ═══════════════════════════════════════════════════

def signal_sideways(closes, highs, lows, price, rsi):
    """
    Стратегия для боковика — Bollinger Bands + RSI-фильтр.

    Long:  цена пробила нижнюю полосу BB + RSI < 50 (не в зоне перекупленности)
    Short: цена пробила верхнюю полосу BB + RSI > 50
    """
    mid, upper, lower = calc_bollinger(closes, BB_PERIOD, BB_STD)
    if mid is None:
        return None, None, None

    prev_close = closes[-2]

    if prev_close <= lower and price > lower and rsi < 55:
        return "Buy", mid, lower   # Long: отскок от нижней полосы
    if prev_close >= upper and price < upper and rsi > 45:
        return "Sell", mid, upper  # Short: отскок от верхней полосы

    return None, None, None


def signal_trend(rsi):
    """
    Стратегия для тренда — адаптивный RSI.
    """
    if rsi < RSI_BUY_CURRENT:
        return "Buy"
    if rsi > RSI_SELL_CURRENT:
        return "Sell"
    return None


# ═══════════════════════════════════════════════════
#   БИРЖЕВЫЕ ФУНКЦИИ
# ═══════════════════════════════════════════════════

def get_instrument_tick(symbol):
    resp = session.get_instruments_info(category="linear", symbol=symbol)
    info = resp["result"]["list"][0]
    return float(info["priceFilter"]["tickSize"])


def round_price(price, tick_size):
    precision = len(str(tick_size).rstrip("0").split(".")[-1]) if "." in str(tick_size) else 0
    return round(round(price / tick_size) * tick_size, precision)


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


def set_leverage(symbol):
    try:
        session.set_leverage(
            category="linear", symbol=symbol,
            buyLeverage=str(LEVERAGE), sellLeverage=str(LEVERAGE),
        )
    except Exception:
        pass


def open_position(symbol, side, qty, price, atr, tick_size, mode):
    """
    Открыть позицию с адаптивным TP/SL на основе ATR.
    Если ATR недоступен — используем фиксированный % (3/2).
    """
    if atr:
        if side == "Buy":
            tp = round_price(price + atr * ATR_TP_MULT, tick_size)
            sl = round_price(price - atr * ATR_SL_MULT, tick_size)
        else:
            tp = round_price(price - atr * ATR_TP_MULT, tick_size)
            sl = round_price(price + atr * ATR_SL_MULT, tick_size)
    else:
        pct_tp, pct_sl = 0.03, 0.02
        if side == "Buy":
            tp = round_price(price * (1 + pct_tp), tick_size)
            sl = round_price(price * (1 - pct_sl), tick_size)
        else:
            tp = round_price(price * (1 - pct_tp), tick_size)
            sl = round_price(price * (1 + pct_sl), tick_size)

    session.place_order(
        category="linear", symbol=symbol,
        side=side, orderType="Market", qty=qty,
        takeProfit=str(tp), stopLoss=str(sl),
        tpTriggerBy="MarkPrice", slTriggerBy="MarkPrice",
    )
    label = "LONG" if side == "Buy" else "SHORT"
    log.info(f"[{symbol}] {label} открыт | режим:{mode} | TP:{tp} SL:{sl}")
    tg.notify_position_opened(symbol, side, qty, price, tp, sl)
    # Сохраняем данные для записи при закрытии
    coin_data[symbol]["open_price"] = price
    coin_data[symbol]["open_side"]  = side
    coin_data[symbol]["open_mode"]  = mode


def close_position(symbol, side, qty, pnl=0.0, result="signal", exit_price=None):
    cancel_orders(symbol)
    close_side = "Sell" if side == "Buy" else "Buy"
    session.place_order(
        category="linear", symbol=symbol,
        side=close_side, orderType="Market",
        qty=qty, reduceOnly=True,
    )
    log.info(f"[{symbol}] Позиция закрыта | PnL:{pnl:+.4f}")
    tg.notify_position_closed(symbol, side, pnl)
    # Записать сделку в базу
    tracker.record_trade(
        symbol=symbol,
        side=side,
        result=result,
        pnl=pnl,
        entry_price=coin_data.get(symbol, {}).get("open_price"),
        exit_price=exit_price or coin_data.get(symbol, {}).get("price"),
        mode=coin_data.get(symbol, {}).get("open_mode"),
    )


# ═══════════════════════════════════════════════════
#   АДАПТАЦИЯ RSI ПОРОГОВ
# ═══════════════════════════════════════════════════

def update_env_rsi(new_buy, new_sell):
    env_path = ".env"
    try:
        with open(env_path, "r", encoding="utf-8") as f:
            lines = f.readlines()
        new_lines = []
        found_buy = found_sell = False
        for line in lines:
            if line.startswith("RSI_BUY="):
                new_lines.append(f"RSI_BUY={new_buy}\n")
                found_buy = True
            elif line.startswith("RSI_SELL="):
                new_lines.append(f"RSI_SELL={new_sell}\n")
                found_sell = True
            else:
                new_lines.append(line)
        if not found_buy:
            new_lines.append(f"RSI_BUY={new_buy}\n")
        if not found_sell:
            new_lines.append(f"RSI_SELL={new_sell}\n")
        with open(env_path, "w", encoding="utf-8") as f:
            f.writelines(new_lines)
    except Exception as e:
        log.warning(f"Не удалось обновить .env: {e}")


def adapt_rsi_thresholds(coins):
    global RSI_BUY_CURRENT, RSI_SELL_CURRENT

    rsi_values = [c["rsi"] for c in coins if c.get("rsi") is not None]
    if not rsi_values:
        return

    avg_rsi = round(sum(rsi_values) / len(rsi_values), 1)
    old_buy, old_sell = RSI_BUY_CURRENT, RSI_SELL_CURRENT

    if avg_rsi < 40:
        new_buy  = round(min(avg_rsi + 15, 50), 1)
        new_sell = round(max(new_buy + 15, 55), 1)
    elif avg_rsi > 60:
        new_sell = round(max(avg_rsi - 15, 50), 1)
        new_buy  = round(min(new_sell - 15, 50), 1)
    else:
        new_buy, new_sell = 45.0, 55.0

    new_buy  = round(max(30, min(new_buy,  50)), 1)
    new_sell = round(max(50, min(new_sell, 70)), 1)

    if new_buy != old_buy or new_sell != old_sell:
        RSI_BUY_CURRENT  = new_buy
        RSI_SELL_CURRENT = new_sell
        update_env_rsi(new_buy, new_sell)
        log.info(f"RSI адаптирован: BUY {old_buy}→{new_buy}, SELL {old_sell}→{new_sell} (avg RSI={avg_rsi})")
        tg.send_message(
            f"🔧 <b>RSI пороги адаптированы</b>\n"
            f"Средний RSI рынка: {avg_rsi}\n"
            f"BUY:  {old_buy} → <b>{new_buy}</b>\n"
            f"SELL: {old_sell} → <b>{new_sell}</b>"
        )


# ═══════════════════════════════════════════════════
#   СТАТУС
# ═══════════════════════════════════════════════════

def save_status():
    alive = [s for s, t in active_bots.items() if t.is_alive()]
    data = {
        "active_bots": alive,
        "max_bots": MAX_BOTS,
        "daily_pnl": daily_pnl,
        "daily_loss_limit": DAILY_LOSS_LIMIT,
        "coins": coin_data,
        "rsi_buy": RSI_BUY_CURRENT,
        "rsi_sell": RSI_SELL_CURRENT,
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    try:
        with open(STATUS_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        log.warning(f"Не удалось сохранить статус: {e}")


# ═══════════════════════════════════════════════════
#   ПОТОК БОТА ДЛЯ ОДНОЙ МОНЕТЫ
# ═══════════════════════════════════════════════════

def bot_thread(symbol: str, stop_event: threading.Event):
    global daily_pnl

    log.info(f"[{symbol}] ▶ Бот запущен")
    set_leverage(symbol)

    try:
        tick_size = get_instrument_tick(symbol)
    except Exception as e:
        log.error(f"[{symbol}] Не удалось получить tickSize: {e}")
        return

    while not stop_event.is_set():
        try:
            closes, highs, lows = get_klines(symbol, INTERVAL)
            price = closes[-1]
            rsi   = calc_rsi(closes, RSI_PERIOD)
            atr   = calc_atr(closes, highs, lows, ATR_PERIOD)
            mode, atr_val, atr_pct = detect_market_mode(closes, highs, lows, price)

            pos      = get_position(symbol)
            pos_side = pos["side"] if pos else None
            pos_qty  = pos["size"] if pos else QTY
            pnl      = float(pos["unrealisedPnl"]) if pos else 0.0

            log.info(
                f"[{symbol}] Цена:{price:.6f} RSI:{rsi} ATR%:{atr_pct} "
                f"Режим:{mode} Поз:{pos_side or 'нет'} PnL:{pnl:+.4f}"
            )

            coin_data[symbol] = {
                "price": price, "rsi": rsi,
                "atr_pct": atr_pct, "mode": mode,
                "position": pos_side or "нет", "pnl": pnl,
            }
            save_status()

            # Лимит потерь
            if daily_pnl <= DAILY_LOSS_LIMIT:
                log.warning(f"[{symbol}] ⛔ Дневной лимит потерь!")
                tg.notify_daily_limit(symbol)
                if pos_side:
                    close_position(symbol, pos_side, pos_qty, pnl)
                break

            # ── Сигналы в зависимости от режима рынка ──
            signal = None

            # Проверка блокировки новых входов (просадка)
            if block_new_trades and not pos_side:
                log.info(f"[{symbol}] ⛔ Новые входы заблокированы адаптером")
            elif mode == "sideways":
                signal, bb_mid, bb_edge = signal_sideways(closes, highs, lows, price, rsi)
                if signal and pos_side and pos_side != signal:
                    daily_pnl += pnl
                    close_position(symbol, pos_side, pos_qty, pnl)
                    time.sleep(1)
                    pos_side = None
                if signal and pos_side is None:
                    open_position(symbol, signal, QTY, price, atr, tick_size, mode)

            else:  # trend
                signal = signal_trend(rsi)
                if signal and pos_side and pos_side != signal:
                    daily_pnl += pnl
                    close_position(symbol, pos_side, pos_qty, pnl)
                    time.sleep(1)
                    pos_side = None
                if signal and pos_side is None:
                    open_position(symbol, signal, QTY, price, atr, tick_size, mode)

            if not signal:
                log.info(f"[{symbol}] Сигнала нет, ждём...")

        except Exception as e:
            log.error(f"[{symbol}] Ошибка: {e}")

        stop_event.wait(timeout=SLEEP_SEC)

    log.info(f"[{symbol}] ⏹ Бот остановлен")


# ═══════════════════════════════════════════════════
#   МЕНЕДЖЕР БОТОВ
# ═══════════════════════════════════════════════════

def start_bot(symbol: str):
    if symbol in active_bots and active_bots[symbol].is_alive():
        return
    event  = threading.Event()
    thread = threading.Thread(
        target=bot_thread, args=(symbol, event),
        name=f"bot-{symbol}", daemon=True,
    )
    stop_flags[symbol]  = event
    active_bots[symbol] = thread
    thread.start()
    log.info(f"✅ Запущен бот: {symbol}")
    tg.notify_bot_started(symbol)


def stop_bot(symbol: str):
    if symbol in stop_flags:
        stop_flags[symbol].set()
    if symbol in active_bots:
        active_bots[symbol].join(timeout=5)
        del active_bots[symbol]
        del stop_flags[symbol]
    coin_data.pop(symbol, None)
    log.info(f"🛑 Остановлен бот: {symbol}")
    tg.notify_bot_stopped(symbol)


def stop_all_bots():
    for symbol in list(active_bots.keys()):
        stop_bot(symbol)


def print_status():
    alive = [s for s, t in active_bots.items() if t.is_alive()]
    modes = {s: coin_data.get(s, {}).get("mode", "?") for s in alive}
    print(f"\n{'─'*54}")
    print(f"  📊 Боты ({len(alive)}/{MAX_BOTS}): {', '.join(f'{s}({m})' for s,m in modes.items()) or 'нет'}")
    print(f"  💸 PnL: {daily_pnl:+.4f} USDT | RSI: BUY<{RSI_BUY_CURRENT} SELL>{RSI_SELL_CURRENT}")
    print(f"{'─'*54}")


# ═══════════════════════════════════════════════════
#   ГЛАВНЫЙ ЦИКЛ
# ═══════════════════════════════════════════════════

def main():
    global block_new_trades, adapter_counter, \
           RSI_BUY_CURRENT, RSI_SELL_CURRENT

    log.info("=" * 54)
    log.info("  🚀 MANAGER ЗАПУЩЕН (Адаптивная стратегия)")
    log.info(f"  BB{BB_PERIOD}/{BB_STD}σ + RSI + ATR{ATR_PERIOD} | TP×{ATR_TP_MULT} SL×{ATR_SL_MULT}")
    log.info(f"  Макс ботов: {MAX_BOTS} | Лимит потерь: {DAILY_LOSS_LIMIT} USDT")
    log.info("=" * 54)
    tg.notify_manager_started(MAX_BOTS, SCAN_INTERVAL_MIN)

    last_reset_day = datetime.now().strftime("%Y-%m-%d")

    try:
        while True:
            # ── Сброс дневного счётчика в полночь ──────
            today = datetime.now().strftime("%Y-%m-%d")
            if today != last_reset_day:
                global daily_pnl
                daily_pnl      = 0.0
                block_new_trades = False
                last_reset_day = today
                log.info("🌅 Новый день — дневной PnL сброшен, блокировка снята")
                tg.send_message("🌅 <b>Новый день</b>\nДневной PnL сброшен. Боты продолжают работу.")

            # ── Сканирование рынка ─────────────────────
            coins = scan(top_n=MAX_BOTS)
            print_scan_results(coins)
            new_symbols = {c["symbol"] for c in coins}

            # ── RSI адаптация по рынку ─────────────────
            adapt_rsi_thresholds(coins)

            # ── Стратегический адаптер (раз в 3 скана) ─
            adapter_counter += 1
            if adapter_counter % 3 == 0:
                result = adapter.adapt(daily_pnl, RSI_BUY_CURRENT, RSI_SELL_CURRENT)
                block_new_trades   = result["block_new_trades"]
                RSI_BUY_CURRENT    = result["rsi_buy"]
                RSI_SELL_CURRENT   = result["rsi_sell"]
                adapter.notify_if_changed(result)

            # ── Запуск / остановка ботов ───────────────
            for symbol in list(active_bots.keys()):
                if symbol not in new_symbols:
                    log.info(f"📤 {symbol} вышел из топа")
                    stop_bot(symbol)

            for symbol in new_symbols:
                if len([t for t in active_bots.values() if t.is_alive()]) >= MAX_BOTS:
                    break
                start_bot(symbol)

            print_status()
            save_status()

            log.info(f"⏳ Следующее сканирование через {SCAN_INTERVAL_MIN} мин...")
            time.sleep(SCAN_INTERVAL_MIN * 60)

    except KeyboardInterrupt:
        log.info("⛔ Остановка...")
    finally:
        stop_all_bots()
        tg.notify_manager_stopped()
        log.info("✅ Всё остановлено.")


if __name__ == "__main__":
    main()
