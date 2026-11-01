#!/usr/bin/env python3
"""
paper_trader.py — БУМАЖНАЯ торговля стратегией пробоя (BTC + ETH, 4h). Реальных ордеров НЕТ.

* Ключи API не используются: только публичные свечи, funding и цены Bybit.
* Логика 1-в-1 как в backtest_breakout.py (индикаторы импортируются оттуда), чтобы через
  2–3 месяца сверить бумажные сделки с бэктестом за тот же период — сделка в сделку.
* Вход: сигнал на закрытии 4h-бара → вход по цене открытия следующего бара (+ проскальзывание).
  Стоп / гэп / трейлинг проверяются по High/Low закрытых баров — так же, как в бэктесте.
* Дополнительно пишется live_px — реальная цена в момент обнаружения сигнала,
  чтобы потом оценить настоящее проскальзывание.
* Результат в R (1R = риск на сделку).

Запуск:   python paper_trader.py            — сервис (цикл)
          python paper_trader.py --report   — отчёт в консоль
Файлы рядом со скриптом: paper_state.json, paper_trades.jsonl, paper.log
"""
import os, sys, time, json, argparse, traceback
from datetime import datetime, timezone

os.environ.setdefault("BO_INTERVAL", "240")
os.environ.setdefault("BO_DONCH", "30")
os.environ.setdefault("BO_SLIPPAGE", "0.0002")
import requests
import backtest_breakout as bb

try:
    from dotenv import load_dotenv
    load_dotenv(os.getenv("PAPER_ENV", "/root/bybit-memecoin-bot/.env"))
except ImportError:
    pass

SYMBOLS  = [s.strip() for s in os.getenv("PAPER_SYMBOLS", "BTCUSDT,ETHUSDT").split(",") if s.strip()]
TG_TOKEN = os.getenv("TELEGRAM_TOKEN")
TG_CHAT  = os.getenv("TELEGRAM_CHAT_ID")
DIR      = os.path.dirname(os.path.abspath(__file__))
STATE_F  = os.path.join(DIR, "paper_state.json")
TRADES_F = os.path.join(DIR, "paper_trades.jsonl")
LOG_F    = os.path.join(DIR, "paper.log")
STEP     = bb.STEP_MS
SETTLE   = 20_000                 # ждём 20 с после закрытия бара, чтобы биржа отдала финальную свечу
POLL_S   = 30
SUMMARY_HOUR_UTC = 6              # ежедневная сводка ≈ 09:00 МСК
EXPECT   = dict(avg_R=0.29, win=35.0, per_year=57)   # BTC+ETH, бэктест 5 лет — для сравнения

# ── служебное ───────────────────────────────────────────────────
def utc(ms=None):
    return datetime.fromtimestamp((ms if ms is not None else time.time() * 1000) / 1000, timezone.utc)

def log(msg):
    line = f"{utc():%Y-%m-%d %H:%M:%S} {msg}"
    print(line, flush=True)
    with open(LOG_F, "a") as f: f.write(line + "\n")

def tg(text):
    if not TG_TOKEN or not TG_CHAT:
        log("TG не настроен, сообщение только в лог"); return
    try:
        requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                      json=dict(chat_id=TG_CHAT, text="📄 БУМАГА\n" + text), timeout=10)
    except Exception as e:
        log(f"TG ошибка: {e}")

def load_state():
    try:
        with open(STATE_F) as f: return json.load(f)
    except FileNotFoundError:
        return {"symbols": {}, "last_summary": "", "started": int(time.time() * 1000)}

def save_state(st):
    tmp = STATE_F + ".tmp"
    with open(tmp, "w") as f: json.dump(st, f, indent=1)
    os.replace(tmp, STATE_F)

def load_trades():
    try:
        with open(TRADES_F) as f: return [json.loads(x) for x in f if x.strip()]
    except FileNotFoundError:
        return []

def fmt(p):
    return f"{p:,.2f}" if p >= 100 else f"{p:.4f}"

# ── данные (публичные, без ключей) ─────────────────────────────
def get_bars(symbol):
    rows = bb._get("/v5/market/kline", category="linear", symbol=symbol, interval=bb.INTERVAL, limit=1000)
    return sorted([(int(x[0]), float(x[1]), float(x[2]), float(x[3]), float(x[4]), float(x[5])) for x in rows])

def get_last_price(symbol):
    return float(bb._get("/v5/market/tickers", category="linear", symbol=symbol)[0]["lastPrice"])

def get_funding(symbol, now_ms):
    return bb.fetch_funding(symbol, now_ms - 3 * 86_400_000)

# ── ядро: обработка новых закрытых баров (повторяет bb.run) ─────
def process(symbol, st, now_ms, bars, funding, live_px, events):
    s = st["symbols"].setdefault(symbol, {"last_ts": None, "pos": None, "pending": None})
    closed  = [b for b in bars if b[0] + STEP + SETTLE <= now_ms]
    forming = [b for b in bars if b[0] + STEP + SETTLE > now_ms]
    if len(closed) < bb.WARMUP + 2:
        log(f"{symbol}: мало истории ({len(closed)})"); return
    ts = [b[0] for b in closed]; O = [b[1] for b in closed]; H = [b[2] for b in closed]
    L = [b[3] for b in closed];  C = [b[4] for b in closed]; V = [b[5] for b in closed]

    if s["last_ts"] is None:            # первый запуск: история не торгуется задним числом
        s["last_ts"] = ts[-1]
        log(f"{symbol}: старт с бара {utc(ts[-1]):%Y-%m-%d %H:%M} UTC"); return

    ef, es, at = bb.ema(C, bb.EMA_FAST), bb.ema(C, bb.EMA_SLOW), bb.atr(H, L, C, bb.ATR_P)

    def open_pos(p, entry_open, entry_ts, px_now):
        d = p["d"]
        entry = entry_open * (1 + d * bb.SLIP)
        stop = entry - d * bb.STOP_ATR * p["atr"]
        dist = abs(entry - stop)
        s["pos"] = dict(d=d, entry=entry, stop=stop, init_stop=stop, qty=bb.RISK_USDT / dist,
                        ext=entry, trailed=False, funding=0.0, entry_ts=entry_ts,
                        sig_ts=p["sig_ts"], live_px=px_now)
        s["pending"] = None
        events.append(("open", symbol, dict(s["pos"])))

    def close_pos(i, px, reason):
        p = s["pos"]; d = p["d"]
        exit_px = px * (1 - d * bb.SLIP)
        gross = d * (exit_px - p["entry"]) * p["qty"]
        fees = (p["entry"] + exit_px) * p["qty"] * bb.TAKER
        net = gross - fees + p["funding"]
        tr = dict(symbol=symbol, side="Buy" if d == 1 else "Sell", reason=reason,
                  entry=p["entry"], exit=exit_px, t_open=p["entry_ts"], t_close=ts[i],
                  hours=(ts[i] - p["entry_ts"]) / 3_600_000, fees=fees, funding=p["funding"],
                  R=net / bb.RISK_USDT, R_gross=gross / bb.RISK_USDT, live_px=p.get("live_px"))
        with open(TRADES_F, "a") as f: f.write(json.dumps(tr) + "\n")
        s["pos"] = None
        events.append(("close", symbol, tr))

    for i in range(len(ts)):
        if ts[i] <= s["last_ts"]: continue
        # 1) отложенный вход исполняется по открытию этого бара
        if s["pending"] and s["pending"]["entry_ts"] == ts[i]:
            open_pos(s["pending"], O[i], ts[i], None)
        elif s["pending"] and s["pending"]["entry_ts"] < ts[i]:
            log(f"{symbol}: потерян бар входа, сигнал отменён"); s["pending"] = None
        # 2) ведём позицию на баре i
        p = s["pos"]
        if p and ts[i] >= p["entry_ts"]:
            d = p["d"]
            if ts[i] > p["entry_ts"] and ts[i] in funding:
                p["funding"] -= d * funding[ts[i]] * p["qty"] * O[i]
            if ts[i] > p["entry_ts"] and (O[i] - p["stop"]) * d <= 0:
                close_pos(i, O[i], "gap")
            elif (L[i] if d == 1 else H[i]) * d <= p["stop"] * d:
                close_pos(i, p["stop"], "trail" if p["trailed"] else "stop")
            else:
                p["ext"] = max(p["ext"], H[i]) if d == 1 else min(p["ext"], L[i])
                trail = p["ext"] - d * bb.TRAIL_ATR * at[i]
                if (trail - p["stop"]) * d > 0:
                    p["stop"], p["trailed"] = trail, True
        # 3) сигнал на закрытии бара i
        if not s["pos"] and not s["pending"] and at[i] is not None and es[i] is not None:
            hi = max(H[i - bb.DONCH:i]); lo = min(L[i - bb.DONCH:i])
            vol_ok = V[i] > bb.VOL_MULT * (sum(V[i - 20:i]) / 20)
            d = 0
            if C[i] > hi and ef[i] > es[i] and vol_ok: d = 1
            if C[i] < lo and ef[i] < es[i] and vol_ok: d = -1
            if d:
                side = "Buy" if d == 1 else "Sell"
                same = sum(1 for x in st["symbols"].values() if x.get("pos") and
                           (x["pos"]["d"] == 1) == (d == 1))
                if bb.MAX_SAME_DIR and same >= bb.MAX_SAME_DIR:
                    log(f"{symbol}: сигнал {side} пропущен — лимит {bb.MAX_SAME_DIR} позиций")
                else:
                    s["pending"] = dict(d=d, sig_ts=ts[i], entry_ts=ts[i] + STEP, atr=at[i])
        s["last_ts"] = ts[i]

    # вход по открытию текущего (формирующегося) бара — его цена открытия уже известна
    if s["pending"] and forming and forming[0][0] == s["pending"]["entry_ts"]:
        open_pos(s["pending"], forming[0][1], forming[0][0], live_px)

# ── сообщения и отчёты ─────────────────────────────────────────
def notify(ev):
    kind, sym, x = ev
    if kind == "open":
        side = "ЛОНГ 🟢" if x["d"] == 1 else "ШОРТ 🔴"
        risk_pct = abs(x["entry"] - x["stop"]) / x["entry"] * 100
        msg = (f"Открыл бы {side} {sym}\nВход: {fmt(x['entry'])}\nСтоп: {fmt(x['stop'])} "
               f"({risk_pct:.1f}% от цены = 1R)")
        if x.get("live_px"): msg += f"\nЦена в момент сигнала: {fmt(x['live_px'])}"
        log(msg.replace("\n", " | ")); tg(msg)
    else:
        tot = load_trades()
        sumR = sum(t["R"] for t in tot)
        emoji = "✅" if x["R"] > 0 else "❌"
        names = {"stop": "стоп", "trail": "трейлинг", "gap": "гэп через стоп"}
        msg = (f"{emoji} Закрыл бы {sym} {('лонг' if x['side']=='Buy' else 'шорт')} — {names.get(x['reason'], x['reason'])}\n"
               f"{fmt(x['entry'])} → {fmt(x['exit'])}, держал {x['hours']:.0f} ч\n"
               f"Результат: {x['R']:+.2f}R (funding {x['funding']:+.3f}R)\n"
               f"Всего: {len(tot)} сделок, {sumR:+.2f}R")
        log(msg.replace("\n", " | ")); tg(msg)

def summary_text(st, prices=None):
    tr = load_trades()
    days = max((time.time() * 1000 - st.get("started", time.time() * 1000)) / 86_400_000, 0.01)
    lines = [f"Сводка за {days:.0f} дн. бумажной торговли"]
    for sym, s in st["symbols"].items():
        p = s.get("pos")
        if p:
            side = "лонг" if p["d"] == 1 else "шорт"
            if prices and sym in prices:
                ur = p["d"] * (prices[sym] - p["entry"]) * p["qty"] + p["funding"]
                lines.append(f"• {sym}: {side} от {fmt(p['entry'])}, стоп {fmt(p['stop'])}, сейчас {ur:+.2f}R")
            else:
                lines.append(f"• {sym}: {side} от {fmt(p['entry'])}, стоп {fmt(p['stop'])}")
        else:
            lines.append(f"• {sym}: без позиции" + (" (ждёт входа)" if s.get("pending") else ""))
    if tr:
        s = bb.stats(tr)
        lines.append(f"Закрыто: {s['n']} | win {s['win']:.0f}% | итог {s['R']:+.2f}R | ср. {s['avg']:+.2f}R")
        lines.append(f"Бэктест ожидал: win ~{EXPECT['win']:.0f}%, ср. ~{EXPECT['avg_R']:+.2f}R, "
                     f"~{EXPECT['per_year']*days/365:.0f} сделок за этот срок")
        lines.append("⚠️ Меньше ~50 сделок — шум, выводы о прибыльности делать рано.")
    else:
        lines.append(f"Закрытых сделок пока нет (бэктест ожидал ~{EXPECT['per_year']*days/365:.1f}).")
    return "\n".join(lines)

def report():
    st = load_state()
    print(summary_text(st))
    tr = load_trades()
    if tr:
        print()
        for t in tr:
            print(f"{utc(t['t_open']):%Y-%m-%d %H:%M} → {utc(t['t_close']):%Y-%m-%d %H:%M}  {t['symbol']:<9} "
                  f"{t['side']:<4} {t['reason']:<6} {t['R']:+.2f}R")

# ── цикл ───────────────────────────────────────────────────────
def main_loop():
    st = load_state(); save_state(st)
    log(f"Старт бумажной торговли: {', '.join(SYMBOLS)} | {bb.INTERVAL}m, канал {bb.DONCH}, "
        f"стоп {bb.STOP_ATR}ATR, трейлинг {bb.TRAIL_ATR}ATR")
    tg(f"Запущена бумажная торговля {', '.join(SYMBOLS)} (4h пробой). Реальных ордеров нет.")
    last_err = 0
    while True:
        now = int(time.time() * 1000)
        latest_closed = (now - SETTLE) // STEP * STEP - STEP
        need = [sym for sym in SYMBOLS
                if st["symbols"].get(sym, {}).get("last_ts") is None
                or st["symbols"][sym]["last_ts"] < latest_closed
                or st["symbols"][sym].get("pending")]
        events = []
        for sym in need:
            try:
                px = get_last_price(sym)
                process(sym, st, now, get_bars(sym), get_funding(sym, now), px, events)
            except Exception as e:
                log(f"{sym}: ошибка {e}\n{traceback.format_exc()}")
                if time.time() - last_err > 3600:
                    tg(f"Ошибка по {sym}: {e}"); last_err = time.time()
        save_state(st)
        for ev in events: notify(ev)

        today = utc().strftime("%Y-%m-%d")
        if utc().hour >= SUMMARY_HOUR_UTC and st.get("last_summary") != today:
            try:
                prices = {s: get_last_price(s) for s in st["symbols"] if st["symbols"][s].get("pos")}
            except Exception:
                prices = None
            tg(summary_text(st, prices)); st["last_summary"] = today; save_state(st)
        time.sleep(POLL_S)

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--report", action="store_true")
    a = ap.parse_args()
    report() if a.report else main_loop()
