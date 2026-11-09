"""
╔══════════════════════════════════════════════════════╗
║  LIVE_TRADER.PY — РЕАЛЬНАЯ торговля BTC/ETH,         ║
║  стратегия пробоя 4h (та же, что paper_trader.py)    ║
║                                                      ║
║  • Сигналы и индикаторы — из backtest_breakout.py,   ║
║    параметры как у бумажного бота: 4h, канал 30,     ║
║    EMA50/200, объём ×1.5, стоп 2×ATR, трейлинг 3×ATR ║
║  • Вход по рынку сразу после закрытия 4h-свечи       ║
║  • Стоп-лосс стоит НА БИРЖЕ с момента входа,         ║
║    трейлинг подтягивается на каждом закрытии 4h      ║
║  • Размер: риск = capital × risk_pct% на сделку      ║
║  • Аварийный стоп: убыток ≥ max_drawdown_pct%        ║
║  • Настройки — файл LIVE_BREAKOUT.json в репозитории ║
║  Работает потоком внутри manager.py.                 ║
╚══════════════════════════════════════════════════════╝
"""

import os
import json
import math
import time
import threading
import traceback
from decimal import Decimal
from datetime import datetime, timezone

# параметры стратегии — ровно как у paper_trader.py (до импорта модуля)
os.environ.setdefault("BO_INTERVAL", "240")
os.environ.setdefault("BO_DONCH", "30")
os.environ.setdefault("BO_SLIPPAGE", "0.0002")
import requests
import backtest_breakout as bb

CONFIG_FILE = "LIVE_BREAKOUT.json"
STATE_F     = "logs/live_state.json"
TRADES_F    = "logs/live_trades.jsonl"
LOG_F       = "logs/live_trader.log"
STEP        = bb.STEP_MS
SETTLE      = 20_000       # ждём 20 с после закрытия свечи
POLL_S      = 30

DEFAULT_CFG = {"enabled": False, "symbols": ["BTCUSDT", "ETHUSDT"], "capital_usdt": 300,
               "risk_pct": 1.0, "max_drawdown_pct": 15, "max_risk_mult": 2.0,
               "leverage": 2, "resume_after_kill": False}

_lock = threading.Lock()


# ═══════════════ служебное ═══════════════

def utc(ms=None):
    return datetime.fromtimestamp((ms if ms is not None else time.time() * 1000) / 1000, timezone.utc)


def log(msg):
    line = f"{utc():%Y-%m-%d %H:%M:%S} {msg}"
    print(line, flush=True)
    os.makedirs("logs", exist_ok=True)
    with open(LOG_F, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def tg(text):
    token, chat = os.getenv("TELEGRAM_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat:
        return
    try:
        requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                      json={"chat_id": chat, "text": "💵 РЕАЛ BTC/ETH\n" + text}, timeout=10)
    except Exception as e:
        log(f"TG ошибка: {e}")


def load_config():
    cfg = dict(DEFAULT_CFG)
    try:
        with open(CONFIG_FILE, encoding="utf-8") as f:
            cfg.update(json.load(f))
    except FileNotFoundError:
        pass
    return cfg


def load_state():
    try:
        with open(STATE_F, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {"symbols": {}, "realized_usdt": 0.0, "killed": False,
                "started": int(time.time() * 1000)}


def save_state(st):
    os.makedirs("logs", exist_ok=True)
    tmp = STATE_F + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(st, f, indent=1)
    os.replace(tmp, STATE_F)


def load_trades():
    try:
        with open(TRADES_F, encoding="utf-8") as f:
            return [json.loads(x) for x in f if x.strip()]
    except FileNotFoundError:
        return []


def fmt(p):
    return f"{p:,.2f}" if p >= 100 else f"{p:.4f}"


def _prec(step):
    e = Decimal(str(step)).normalize().as_tuple().exponent
    return max(-e, 0) if isinstance(e, int) else 0


def round_price(px, tick):
    return round(round(px / tick) * tick, _prec(tick))


def floor_qty(q, step):
    return round(math.floor(q / step + 1e-9) * step, _prec(step))


# ═══════════════ биржа ═══════════════

def get_bars(symbol):
    rows = bb._get("/v5/market/kline", category="linear", symbol=symbol, interval=bb.INTERVAL, limit=1000)
    return sorted([(int(x[0]), float(x[1]), float(x[2]), float(x[3]), float(x[4]), float(x[5])) for x in rows])


def instrument(session, symbol):
    info = session.get_instruments_info(category="linear", symbol=symbol)["result"]["list"][0]
    return (float(info["priceFilter"]["tickSize"]), float(info["lotSizeFilter"]["qtyStep"]),
            float(info["lotSizeFilter"]["minOrderQty"]))


def exch_position(session, symbol):
    for p in session.get_positions(category="linear", symbol=symbol)["result"]["list"]:
        if float(p.get("size") or 0) > 0:
            return p
    return None


def closed_pnl_since(session, symbol, since_ms):
    rows = session.get_closed_pnl(category="linear", symbol=symbol, limit=20)["result"]["list"]
    rows = [r for r in rows if int(r.get("updatedTime") or 0) >= since_ms]
    if not rows:
        return None
    qty = sum(float(r.get("closedSize") or r.get("qty") or 0) for r in rows) or 1.0
    exit_px = sum(float(r.get("avgExitPrice") or 0) * float(r.get("closedSize") or r.get("qty") or 0)
                  for r in rows) / qty
    return {"pnl": sum(float(r.get("closedPnl") or 0) for r in rows), "exit": exit_px}


# ═══════════════ логика ═══════════════

def risk_usdt(cfg):
    return cfg["capital_usdt"] * cfg["risk_pct"] / 100


def entries_allowed(cfg, st):
    if not cfg.get("enabled"):
        return False, "торговля выключена (enabled=false)"
    if st.get("killed") and not cfg.get("resume_after_kill"):
        return False, "сработал аварийный стоп"
    return True, ""


def open_position(session, cfg, st, symbol, d, atr_v, sig_ts, entry_ts):
    s = st["symbols"][symbol]
    tick, step, minq = instrument(session, symbol)
    stop_dist = bb.STOP_ATR * atr_v
    risk = risk_usdt(cfg)
    qty = floor_qty(risk / stop_dist, step)
    if qty < minq:
        if minq * stop_dist <= risk * cfg["max_risk_mult"]:
            qty = minq
        else:
            msg = (f"Сигнал {'ЛОНГ' if d == 1 else 'ШОРТ'} {symbol} пропущен: минимальный лот {minq} "
                   f"даёт риск {minq * stop_dist:.2f}$ при допустимых {risk * cfg['max_risk_mult']:.2f}$")
            log(msg); tg(msg); return
    side = "Buy" if d == 1 else "Sell"
    try:
        session.set_leverage(category="linear", symbol=symbol,
                             buyLeverage=str(cfg["leverage"]), sellLeverage=str(cfg["leverage"]))
    except Exception:
        pass
    px = float(bb._get("/v5/market/tickers", category="linear", symbol=symbol)[0]["lastPrice"])
    stop_guess = round_price(px - d * stop_dist, tick)
    session.place_order(category="linear", symbol=symbol, side=side, orderType="Market",
                        qty=str(qty), stopLoss=str(stop_guess), slTriggerBy="LastPrice",
                        tpslMode="Full", positionIdx=0)
    time.sleep(1.5)
    p = exch_position(session, symbol)
    if not p:
        msg = f"⚠️ Ордер {side} {symbol} отправлен, но позиция не появилась — проверь биржу"
        log(msg); tg(msg); return
    entry = float(p.get("avgPrice") or px)
    stop = round_price(entry - d * stop_dist, tick)
    if stop != stop_guess:
        session.set_trading_stop(category="linear", symbol=symbol, stopLoss=str(stop),
                                 slTriggerBy="LastPrice", tpslMode="Full", positionIdx=0)
    real_risk = float(p.get("size") or qty) * abs(entry - stop)
    s["pos"] = {"d": d, "entry": entry, "stop": stop, "init_stop": stop, "qty": float(p.get("size") or qty),
                "risk_usdt": real_risk, "ext": entry, "trailed": False, "entry_ts": entry_ts,
                "sig_ts": sig_ts, "opened_ms": int(time.time() * 1000), "signal_px": px, "tick": tick}
    msg = (f"Открыл {'ЛОНГ 🟢' if d == 1 else 'ШОРТ 🔴'} {symbol}\n"
           f"Вход: {fmt(entry)} (цена при сигнале {fmt(px)})\n"
           f"Стоп на бирже: {fmt(stop)} ({abs(entry - stop) / entry * 100:.1f}%)\n"
           f"Объём: {s['pos']['qty']} (~{s['pos']['qty'] * entry:.0f}$), риск {real_risk:.2f}$ = 1R")
    log(msg.replace("\n", " | ")); tg(msg)


def handle_closed(session, cfg, st, symbol):
    s = st["symbols"][symbol]; p = s["pos"]
    res = closed_pnl_since(session, symbol, p["opened_ms"] - 60_000)
    if res is None:
        log(f"{symbol}: позиция закрыта, но запись о PnL ещё не пришла — проверю на следующем цикле")
        return
    R = res["pnl"] / p["risk_usdt"] if p["risk_usdt"] else 0.0
    tr = {"symbol": symbol, "side": "Buy" if p["d"] == 1 else "Sell",
          "reason": "trail" if p["trailed"] else "stop", "entry": p["entry"], "exit": res["exit"],
          "t_open": p["entry_ts"], "t_close": int(time.time() * 1000),
          "hours": (time.time() * 1000 - p["opened_ms"]) / 3_600_000,
          "pnl_usdt": res["pnl"], "R": R, "risk_usdt": p["risk_usdt"]}
    with open(TRADES_F, "a", encoding="utf-8") as f:
        f.write(json.dumps(tr) + "\n")
    s["pos"] = None
    st["realized_usdt"] = st.get("realized_usdt", 0.0) + res["pnl"]
    trades = load_trades()
    total_r = sum(t["R"] for t in trades)
    msg = (f"{'✅' if res['pnl'] > 0 else '❌'} Закрыта {symbol} {'лонг' if p['d'] == 1 else 'шорт'} — "
           f"{'трейлинг' if p['trailed'] else 'стоп'}\n"
           f"{fmt(p['entry'])} → {fmt(res['exit'])}, держал {tr['hours']:.0f} ч\n"
           f"Результат: {res['pnl']:+.2f}$ ({R:+.2f}R)\n"
           f"Всего: {len(trades)} сделок, {st['realized_usdt']:+.2f}$ ({total_r:+.2f}R)")
    log(msg.replace("\n", " | ")); tg(msg)
    limit = -cfg["capital_usdt"] * cfg["max_drawdown_pct"] / 100
    if st["realized_usdt"] <= limit and not st.get("killed"):
        st["killed"] = True
        msg = (f"⛔ АВАРИЙНЫЙ СТОП: убыток {st['realized_usdt']:.2f}$ достиг лимита {limit:.0f}$ "
               f"({cfg['max_drawdown_pct']}% от {cfg['capital_usdt']}$). Новые сделки не открываются.")
        log(msg); tg(msg)


def process(session, cfg, st, symbol, now_ms):
    s = st["symbols"].setdefault(symbol, {"last_ts": None, "pos": None})
    # 1) позиция закрылась на бирже (стоп/трейлинг сработал)?
    if s["pos"] and exch_position(session, symbol) is None:
        handle_closed(session, cfg, st, symbol)
        if s["pos"]:
            return
    latest_closed = (now_ms - SETTLE) // STEP * STEP - STEP
    if s["last_ts"] is not None and s["last_ts"] >= latest_closed:
        return                     # новой закрытой 4h-свечи ещё нет
    bars = get_bars(symbol)
    closed = [b for b in bars if b[0] + STEP + SETTLE <= now_ms]
    if len(closed) < bb.WARMUP + 2:
        log(f"{symbol}: мало истории ({len(closed)})"); return
    ts = [b[0] for b in closed]; H = [b[2] for b in closed]; L = [b[3] for b in closed]
    C = [b[4] for b in closed]; V = [b[5] for b in closed]
    if s["last_ts"] is None:
        s["last_ts"] = ts[-1]
        if not s["pos"] and exch_position(session, symbol):
            msg = f"⚠️ {symbol}: на бирже уже есть позиция, открытая не этим ботом — по {symbol} не торгую"
            log(msg); tg(msg); s["foreign"] = True
        log(f"{symbol}: старт с бара {utc(ts[-1]):%Y-%m-%d %H:%M} UTC"); return
    if ts[-1] <= s["last_ts"]:
        return
    if s.get("foreign"):
        if exch_position(session, symbol) is None:
            s["foreign"] = False
        else:
            s["last_ts"] = ts[-1]; return
    ef, es, at = bb.ema(C, bb.EMA_FAST), bb.ema(C, bb.EMA_SLOW), bb.atr(H, L, C, bb.ATR_P)
    # 2) трейлинг по новым закрытым свечам — как в бэктесте
    p = s["pos"]
    if p:
        d, new_stop = p["d"], p["stop"]
        for i in range(len(ts)):
            if ts[i] <= s["last_ts"] or ts[i] < p["entry_ts"] or at[i] is None:
                continue
            p["ext"] = max(p["ext"], H[i]) if d == 1 else min(p["ext"], L[i])
            trail = p["ext"] - d * bb.TRAIL_ATR * at[i]
            if (trail - new_stop) * d > 0:
                new_stop = trail
        new_stop = round_price(new_stop, p["tick"])
        if (new_stop - p["stop"]) * d > 0:
            session.set_trading_stop(category="linear", symbol=symbol, stopLoss=str(new_stop),
                                     slTriggerBy="LastPrice", tpslMode="Full", positionIdx=0)
            log(f"{symbol}: трейлинг {fmt(p['stop'])} → {fmt(new_stop)}")
            p["stop"], p["trailed"] = new_stop, True
    # 3) сигнал на последней закрытой свече → вход сейчас (≈ открытие следующей)
    i = len(ts) - 1
    s["last_ts"] = ts[i]
    if s["pos"] or at[i] is None or es[i] is None:
        return
    hi = max(H[i - bb.DONCH:i]); lo = min(L[i - bb.DONCH:i])
    vol_ok = V[i] > bb.VOL_MULT * (sum(V[i - 20:i]) / 20)
    d = 1 if (C[i] > hi and ef[i] > es[i] and vol_ok) else (-1 if (C[i] < lo and ef[i] < es[i] and vol_ok) else 0)
    if not d:
        return
    ok, why = entries_allowed(cfg, st)
    if not ok:
        msg = f"Сигнал {'ЛОНГ' if d == 1 else 'ШОРТ'} {symbol} пропущен: {why}"
        log(msg); tg(msg); return
    same = sum(1 for x in st["symbols"].values() if x.get("pos") and x["pos"]["d"] == d)
    if bb.MAX_SAME_DIR and same >= bb.MAX_SAME_DIR:
        return
    open_position(session, cfg, st, symbol, d, at[i], ts[i], ts[i] + STEP)


def loop(session):
    log(f"Старт реальной торговли: 4h пробой, канал {bb.DONCH}, стоп {bb.STOP_ATR}ATR, трейлинг {bb.TRAIL_ATR}ATR")
    last_err = 0.0
    announced = None
    while True:
        cfg = load_config()
        with _lock:
            st = load_state()
            if announced != cfg.get("enabled"):
                announced = cfg.get("enabled")
                tg(f"{'▶️ Торговля включена' if announced else '⏸ Новые входы выключены'}: "
                   f"{', '.join(cfg['symbols'])}, капитал {cfg['capital_usdt']}$, риск "
                   f"{risk_usdt(cfg):.2f}$ на сделку, аварийный стоп −{cfg['max_drawdown_pct']}%")
            for sym in cfg["symbols"]:
                try:
                    process(session, cfg, st, sym, int(time.time() * 1000))
                except Exception as e:
                    log(f"{sym}: ошибка {e}\n{traceback.format_exc()}")
                    if time.time() - last_err > 3600:
                        tg(f"⚠️ Ошибка по {sym}: {e}"); last_err = time.time()
            save_state(st)
        time.sleep(POLL_S)


def start(session):
    """Запускается из manager.py. Поток стартует, если торговля включена
    или есть открытая позиция, которую надо сопровождать."""
    cfg = load_config()
    st = load_state()
    has_pos = any(x.get("pos") for x in st.get("symbols", {}).values())
    if not cfg.get("enabled") and not has_pos:
        log("Живая торговля BTC/ETH выключена (LIVE_BREAKOUT.json)")
        return None
    th = threading.Thread(target=lambda: _safe_loop(session), name="live-breakout", daemon=True)
    th.start()
    return th


def _safe_loop(session):
    while True:
        try:
            loop(session)
        except Exception as e:
            log(f"live loop упал: {e}\n{traceback.format_exc()}")
            time.sleep(60)


def status_text():
    cfg, st, trades = load_config(), load_state(), load_trades()
    lines = [f"💵 <b>Реальная торговля BTC/ETH</b> (4h пробой)",
             f"Статус: {'включена' if cfg.get('enabled') else 'новые входы выключены'}"
             + (" | ⛔ аварийный стоп" if st.get("killed") else ""),
             f"Капитал {cfg['capital_usdt']}$, риск {risk_usdt(cfg):.2f}$ на сделку, "
             f"лимит убытка −{cfg['capital_usdt'] * cfg['max_drawdown_pct'] / 100:.0f}$", ""]
    for sym in cfg["symbols"]:
        p = st.get("symbols", {}).get(sym, {}).get("pos")
        if p:
            lines.append(f"• {sym}: {'лонг' if p['d'] == 1 else 'шорт'} от {fmt(p['entry'])}, "
                         f"стоп {fmt(p['stop'])}{' (трейлинг)' if p['trailed'] else ''}")
        else:
            lines.append(f"• {sym}: без позиции")
    if trades:
        wins = sum(1 for t in trades if t["pnl_usdt"] > 0)
        lines += ["", f"Закрыто сделок: {len(trades)}, в плюс {wins}",
                  f"Итог: {st.get('realized_usdt', 0):+.2f}$ ({sum(t['R'] for t in trades):+.2f}R)",
                  "Бэктест ожидал: win ~35%, в среднем ~+0.29R на сделку, ~1 сделка в неделю."]
    else:
        lines += ["", "Закрытых сделок пока нет."]
    return "\n".join(lines)
