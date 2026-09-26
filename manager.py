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
import math
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

# ── AI Integration ────────────────────────────────────────
try:
    from hermes_brain import HermesBrain
    HERMES_ENABLED = os.getenv("HERMES_ENABLED", "0") == "1"
    if HERMES_ENABLED:
        brain = HermesBrain()
        log_ai = logging.getLogger("hermes_brain")
        log_ai.info("🧠 Hermes Brain initialized")
    else:
        brain = None
        log_ai = None
except ImportError:
    HERMES_ENABLED = False
    brain = None
    log_ai = None

load_dotenv()

# ── Настройки ────────────────────────────────────────
SCAN_INTERVAL_MIN = int(float(os.getenv("SCAN_INTERVAL_MIN", "30")))
MAX_BOTS          = int(float(os.getenv("MAX_BOTS",          "3")))
DAILY_LOSS_LIMIT  = float(os.getenv("DAILY_LOSS_LIMIT","-50"))
LEVERAGE          = int(float(os.getenv("LEVERAGE",          "2")))  # int(float()) — терпит "2.0" из оптимизатора
QTY_USDT_BASE     = float(os.getenv("QTY_USDT",        "50"))  # базовый размер позиции в USDT (было 20)
INTERVAL          = os.getenv("INTERVAL",  "5")
RSI_PERIOD        = int(float(os.getenv("RSI_PERIOD", "14")))
SLEEP_SEC         = int(float(os.getenv("SLEEP_SEC",  "60")))

# ── AI Hard Limits (cannot be exceeded by AI) ────────────
MAX_POSITIONS     = int(os.getenv("MAX_POSITIONS",     "5"))   # жёсткий лимит позиций
MAX_LEVERAGE      = int(os.getenv("MAX_LEVERAGE",      "2"))   # жёсткий лимит плеча
MIN_POSITION_USDT = float(os.getenv("MIN_POSITION_USDT", "10"))  # минимум размера позиции
MAX_POSITION_USDT = float(os.getenv("MAX_POSITION_USDT", "30"))  # максимум размера позиции

# Bollinger Bands
BB_PERIOD   = int(float(os.getenv("BB_PERIOD",  "20")))
BB_STD      = float(os.getenv("BB_STD",   "2.0"))

# ATR
ATR_PERIOD  = int(float(os.getenv("ATR_PERIOD", "14")))
ATR_TP_MULT = float(os.getenv("ATR_TP_MULT", "2.5"))  # TP = ATR × 2.5 (было 2.0)
ATR_SL_MULT = float(os.getenv("ATR_SL_MULT", "1.2"))  # SL = ATR × 1.2 (было 1.0)

# Режим рынка
SIDEWAYS_ATR_THRESHOLD = float(os.getenv("SIDEWAYS_ATR_THRESHOLD", "0.03"))  # ATR/цена < 3% = боковик

# RSI пороги (адаптируются автоматически)
RSI_BUY_CURRENT  = float(os.getenv("RSI_BUY",  "40"))  # было 45
RSI_SELL_CURRENT = float(os.getenv("RSI_SELL", "60"))  # было 55

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
consecutive_stops: int = 0        # счётчик последовательных стоп-лоссов (для быстрой адаптации)
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
    volumes = [float(c[5]) for c in candles]  # добавлен объём
    return closes, highs, lows, volumes


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

def signal_sideways(closes, highs, lows, price, rsi, volumes=None):
    """
    Стратегия для боковика — Bollinger Bands + RSI-фильтр + объёмный фильтр.

    Long:  цена пробила нижнюю полосу BB + RSI < 50 + всплеск объёма
    Short: цена пробила верхнюю полосу BB + RSI > 50 + всплеск объёма
    
    volumes: опциональный массив объёмов для фильтрации
    """
    mid, upper, lower = calc_bollinger(closes, BB_PERIOD, BB_STD)
    if mid is None:
        return None, None, None

    prev_close = closes[-2]
    
    # Объёмный фильтр (если доступен)
    volume_confirmed = True
    if volumes and len(volumes) >= 20:
        avg_volume = sum(volumes[-20:-1]) / 19  # средний объём за 19 периодов
        current_volume = volumes[-1]
        volume_confirmed = current_volume > avg_volume * 1.3  # всплеск на 30%+

    if prev_close <= lower and price > lower and rsi < 55 and volume_confirmed:
        return "Buy", mid, lower   # Long: отскок от нижней полосы
    if prev_close >= upper and price < upper and rsi > 45 and volume_confirmed:
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

def get_instrument_info(symbol):
    """Получить tick_size и qty_step для символа."""
    resp = session.get_instruments_info(category="linear", symbol=symbol)
    info = resp["result"]["list"][0]
    tick_size = float(info["priceFilter"]["tickSize"])
    qty_step  = float(info["lotSizeFilter"]["qtyStep"])
    min_qty   = float(info["lotSizeFilter"]["minOrderQty"])
    return tick_size, qty_step, min_qty


def calc_qty(price: float, qty_usdt: float, qty_step: float, min_qty: float) -> str:
    """
    Рассчитать размер позиции в монетах из суммы в USDT.
    Гарантирует соответствие qty_step и минимальному размеру.
    Минимум 5 USDT (требование Bybit).
    """
    raw = qty_usdt / price
    # Округляем вниз до qty_step
    stepped = math.floor(raw / qty_step) * qty_step
    # Не меньше минимума
    result = max(stepped, min_qty)
    # Точность как у qty_step
    precision = len(str(qty_step).rstrip("0").split(".")[-1]) if "." in str(qty_step) else 0
    return str(round(result, precision))


def calc_adaptive_qty(price: float, atr_pct: float, qty_step: float, min_qty: float) -> str:
    """
    Адаптивный размер позиции: больше волатильность → меньше риск.
    
    Формула: qty_usdt = QTY_USDT_BASE / (1 + atr_pct * 10)
    Пример: ATR=2% → множитель 1.2 → позиция 20/1.2 = 16.6 USDT
            ATR=5% → множитель 1.5 → позиция 20/1.5 = 13.3 USDT
    """
    if atr_pct is None or atr_pct <= 0:
        atr_pct = 0.02  # дефолт 2%
    
    # Снижаем размер при высокой волатильности
    volume_mult = 1 / (1 + atr_pct * 10)
    qty_usdt = QTY_USDT_BASE * volume_mult
    
    # Минимум 5 USDT (требование Bybit)
    qty_usdt = max(qty_usdt, 5.0)
    
    return calc_qty(price, qty_usdt, qty_step, min_qty)


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


def open_position(symbol, side, price, atr, tick_size, qty_step, min_qty, mode, atr_pct=None):
    """
    Открыть позицию с адаптивным размером и защитой от фандинга.
    Использует Limit IOC для защиты от проскальзывания.
    """
    # 0. AI Veto check (if enabled)
    if HERMES_ENABLED and brain:
        try:
            position_usdt = (float(calc_adaptive_qty(price, atr_pct or 0.02, qty_step, min_qty)) * price) if atr_pct else (QTY_USDT_BASE)
            # Enforce hard limits
            position_usdt = max(MIN_POSITION_USDT, min(position_usdt, MAX_POSITION_USDT))
            
            trade_allowed, reason = brain.should_trade(
                symbol=symbol,
                side="long" if side == "Buy" else "short",
                current_price=price,
                position_usdt=position_usdt,
                leverage=LEVERAGE,
                include_news=True  # анализ новостей перед открытием
            )
            
            if not trade_allowed:
                log.warning(f"[{symbol}] 🧠 AI VETO: {reason}")
                return
            else:
                log.info(f"[{symbol}] 🧠 AI approved: {reason}")
        except Exception as e:
            log.error(f"[{symbol}] ⚠️ AI check failed: {e} — продолжаем без AI")
    
    # 1. Проверка фандинга
    try:
        ticker = session.get_tickers(category="linear", symbol=symbol)
        funding_rate = float(ticker["result"]["list"][0]["fundingRate"])
        if abs(funding_rate) > 0.0005:  # > 0.05% фандинг
            log.warning(f"[{symbol}] Фандинг высокий ({funding_rate:.4%}), пропускаем вход")
            return
    except Exception as e:
        log.warning(f"[{symbol}] Не удалось проверить фандинг: {e}")
    
    # 2. Адаптивный размер позиции
    qty = calc_adaptive_qty(price, atr_pct or 0.02, qty_step, min_qty)

    # Минимальный шаг цены для TP/SL — не менее 0.5% от цены
    min_move = price * 0.005

    if atr and atr > 0:
        tp_move = max(atr * ATR_TP_MULT, min_move * 2)
        sl_move = max(atr * ATR_SL_MULT, min_move)
    else:
        tp_move = price * 0.03   # 3% по умолчанию
        sl_move = price * 0.015  # 1.5% по умолчанию

    if side == "Buy":
        tp = round_price(price + tp_move, tick_size)
        sl = round_price(price - sl_move, tick_size)
    else:
        tp = round_price(price - tp_move, tick_size)
        sl = round_price(price + sl_move, tick_size)

    # Финальная проверка — TP и SL не равны нулю и не равны цене
    if tp <= 0 or sl <= 0 or tp == price or sl == price:
        log.warning(f"[{symbol}] Некорректные TP={tp} SL={sl} — пропускаем сделку")
        return

    # 3. Limit IOC ордер для защиты от проскальзывания (±0.3% толерантность)
    if side == "Buy":
        limit_price = round_price(price * 1.003, tick_size)  # +0.3% для покупки
    else:
        limit_price = round_price(price * 0.997, tick_size)  # -0.3% для продажи
    
    estimated_usdt = float(qty) * price
    log.info(f"[{symbol}] Открываю {'LONG' if side=='Buy' else 'SHORT'} | "
             f"qty:{qty} (~{estimated_usdt:.1f}$) | Limit:{limit_price} | TP:{tp} SL:{sl} | режим:{mode}")

    session.place_order(
        category="linear", symbol=symbol,
        side=side, orderType="Limit", qty=qty,
        price=str(limit_price), timeInForce="IOC",  # Immediate-Or-Cancel
        takeProfit=str(tp), stopLoss=str(sl),
        tpTriggerBy="MarkPrice", slTriggerBy="MarkPrice",
    )

    label = "LONG" if side == "Buy" else "SHORT"
    log.info(f"[{symbol}] ✅ {label} открыт | режим:{mode} | TP:{tp} SL:{sl}")
    tg.notify_position_opened(symbol, side, qty, price, tp, sl)
    coin_data[symbol]["open_price"] = price
    coin_data[symbol]["open_side"]  = side
    coin_data[symbol]["open_mode"]  = mode
    coin_data[symbol]["open_time"]  = time.time()  # 🔧 FIX: запоминаем время открытия


def close_position(symbol, side, qty, pnl=0.0, result="signal", exit_price=None):
    global consecutive_stops
    
    cancel_orders(symbol)
    close_side = "Sell" if side == "Buy" else "Buy"
    session.place_order(
        category="linear", symbol=symbol,
        side=close_side, orderType="Market",
        qty=qty, reduceOnly=True,
    )
    log.info(f"[{symbol}] Позиция закрыта | PnL:{pnl:+.4f}")
    tg.notify_position_closed(symbol, side, pnl)
    
    # Счётчик последовательных стопов
    if pnl < 0:
        consecutive_stops += 1
    else:
        consecutive_stops = 0  # сброс при прибыльной сделке
    
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
        tick_size, qty_step, min_qty = get_instrument_info(symbol)
        log.info(f"[{symbol}] tick:{tick_size} qty_step:{qty_step} min_qty:{min_qty}")
    except Exception as e:
        log.error(f"[{symbol}] Не удалось получить инфо инструмента: {e}")
        return

    while not stop_event.is_set():
        try:
            closes, highs, lows, volumes = get_klines(symbol, INTERVAL)
            price = closes[-1]
            rsi   = calc_rsi(closes, RSI_PERIOD)
            atr   = calc_atr(closes, highs, lows, ATR_PERIOD)
            mode, atr_val, atr_pct = detect_market_mode(closes, highs, lows, price)

            pos      = get_position(symbol)
            pos_side = pos["side"] if pos else None
            pos_qty  = pos["size"] if pos else None
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
                signal, bb_mid, bb_edge = signal_sideways(closes, highs, lows, price, rsi, volumes)
                
                # 🔧 FIX: Умный реверс — закрываем противоположный сигнал ТОЛЬКО в плюсе
                if signal and pos_side and pos_side != signal:
                    if pnl > 0:
                        # 🎯 Фиксируем прибыль при противоположном сигнале
                        log.info(f"[{symbol}] 🎯 Противоположный сигнал + PnL:{pnl:+.4f} → фиксируем прибыль")
                        daily_pnl += pnl
                        close_position(symbol, pos_side, pos_qty, pnl)
                        time.sleep(1)
                        pos_side = None
                    else:
                        log.info(f"[{symbol}] ⏳ Противоположный сигнал, но PnL:{pnl:+.4f} → держим до TP/SL")
                
                if signal and pos_side is None:
                    open_position(symbol, signal, price, atr, tick_size, qty_step, min_qty, mode, atr_pct)

            else:  # trend
                signal = signal_trend(rsi)
                
                # 🔧 FIX: Умный реверс — закрываем противоположный сигнал ТОЛЬКО в плюсе
                if signal and pos_side and pos_side != signal:
                    if pnl > 0:
                        # 🎯 Фиксируем прибыль при противоположном сигнале
                        log.info(f"[{symbol}] 🎯 Противоположный сигнал + PnL:{pnl:+.4f} → фиксируем прибыль")
                        daily_pnl += pnl
                        close_position(symbol, pos_side, pos_qty, pnl)
                        time.sleep(1)
                        pos_side = None
                    else:
                        log.info(f"[{symbol}] ⏳ Противоположный сигнал, но PnL:{pnl:+.4f} → держим до TP/SL")
                
                if signal and pos_side is None:
                    open_position(symbol, signal, price, atr, tick_size, qty_step, min_qty, mode, atr_pct)

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
    global block_new_trades, adapter_counter, consecutive_stops, \
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
                consecutive_stops = 0  # сброс счётчика стопов
                last_reset_day = today
                log.info("🌅 Новый день — дневной PnL сброшен, блокировка снята")
                tg.send_message("🌅 <b>Новый день</b>\nДневной PnL сброшен. Боты продолжают работу.")

            # ── Сканирование рынка ─────────────────────
            coins = scan(top_n=MAX_BOTS)
            print_scan_results(coins)
            new_symbols = {c["symbol"] for c in coins}

            # ── RSI адаптация по рынку ─────────────────
            adapt_rsi_thresholds(coins)

            # ── Стратегический адаптер ─────────────────
            # Быстрая адаптация при 3 последовательных стопах ИЛИ каждые 3 скана
            adapter_counter += 1
            if consecutive_stops >= 3 or adapter_counter % 3 == 0:
                if consecutive_stops >= 3:
                    log.warning(f"⚠️ {consecutive_stops} последовательных стопов → экстренная адаптация")
                    tg.send_message(f"⚠️ <b>Экстренная адаптация</b>\n{consecutive_stops} стопов подряд — корректирую параметры")
                
                result = adapter.adapt(daily_pnl, RSI_BUY_CURRENT, RSI_SELL_CURRENT)
                block_new_trades   = result["block_new_trades"]
                RSI_BUY_CURRENT    = result["rsi_buy"]
                RSI_SELL_CURRENT   = result["rsi_sell"]
                adapter.notify_if_changed(result)
                
                if consecutive_stops >= 3:
                    consecutive_stops = 0  # сброс после адаптации

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
