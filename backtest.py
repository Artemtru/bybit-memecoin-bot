#!/usr/bin/env python3
"""
backtest.py — офлайн-бэктест стратегии manager.py (боковик: BB + RSI + объём,
ATR-TP/SL, ADX-вето, кулдаун после стопа). Только публичные данные Bybit,
ключи не нужны, ордера не ставятся.

Запуск на VPS:
    cd /root/bybit-memecoin-bot
    python3 backtest.py POPCATUSDT WIFUSDT --days 30
    python3 backtest.py --days 14            # монеты берёт из scanner.scan()

Параметры стратегии читаются из .env (те же, что у живого бота),
любой можно переопределить: BB_STD=2.0 python3 backtest.py ...
"""
import argparse, math, os, sys, time, json
from datetime import datetime, timezone

import requests
from dotenv import load_dotenv

load_dotenv()
E = lambda k, d: float(os.getenv(k, d))

INTERVAL      = os.getenv("INTERVAL", "5")
RSI_PERIOD    = int(E("RSI_PERIOD", 14))
BB_PERIOD     = int(E("BB_PERIOD", 20))
BB_STD        = E("BB_STD", 1.5)
ATR_PERIOD    = int(E("ATR_PERIOD", 14))
ATR_TP_MULT   = E("ATR_TP_MULT", 2.5)
ATR_SL_MULT   = E("ATR_SL_MULT", 1.2)
SIDEWAYS_THR  = E("SIDEWAYS_ATR_THRESHOLD", 0.015)
ADX_PERIOD    = int(E("ADX_PERIOD", 14))
ADX_THR       = E("ADX_TREND_THRESHOLD", 25)
COOLDOWN_MIN  = E("STOP_COOLDOWN_MIN", 20)
QTY_USDT_BASE = E("QTY_USDT", 20)
MIN_POS       = E("MIN_POSITION_USDT", 10)
MAX_POS       = E("MAX_POSITION_USDT", 30)
FEE           = E("BT_TAKER_FEE", 0.00055)   # Bybit linear taker, на каждую сторону
WINDOW        = 150                          # как get_klines(limit=150)
BASE          = "https://api.bybit.com"

# ── индикаторы: 1-в-1 из manager.py ─────────────────────────────
def calc_rsi(c, p=14):
    g, l = [], []
    for i in range(1, len(c)):
        d = c[i] - c[i-1]; g.append(max(d, 0)); l.append(max(-d, 0))
    ag, al = sum(g[:p]) / p, sum(l[:p]) / p
    for i in range(p, len(g)):
        ag = (ag*(p-1) + g[i]) / p; al = (al*(p-1) + l[i]) / p
    return 100.0 if al == 0 else round(100 - 100/(1 + ag/al), 2)

def calc_bollinger(c, p, k):
    if len(c) < p: return None, None, None
    w = c[-p:]; m = sum(w)/p; s = (sum((x-m)**2 for x in w)/p) ** 0.5
    return m, m + k*s, m - k*s

def calc_atr(c, h, l, p=14):
    tr = [max(h[i]-l[i], abs(h[i]-c[i-1]), abs(l[i]-c[i-1])) for i in range(1, len(c))]
    if len(tr) < p: return None
    a = sum(tr[:p]) / p
    for t in tr[p:]: a = (a*(p-1) + t) / p
    return a

def calc_ema(c, p):
    if len(c) < p: return None
    e = sum(c[:p]) / p; k = 2/(p+1)
    for x in c[p:]: e = x*k + e*(1-k)
    return e

def calc_adx(c, h, l, p=14):
    n = len(c)
    if n < p*2 + 1: return None
    pdm, mdm, tr = [], [], []
    for i in range(1, n):
        up, dn = h[i]-h[i-1], l[i-1]-l[i]
        pdm.append(up if up > dn and up > 0 else 0.0)
        mdm.append(dn if dn > up and dn > 0 else 0.0)
        tr.append(max(h[i]-l[i], abs(h[i]-c[i-1]), abs(l[i]-c[i-1])))
    def sm(v):
        s = [sum(v[:p])]
        for x in v[p:]: s.append(s[-1] - s[-1]/p + x)
        return s
    dxs = []
    for a, pv, mv in zip(sm(tr), sm(pdm), sm(mdm)):
        if a == 0: continue
        pdi, mdi = 100*pv/a, 100*mv/a
        if pdi + mdi == 0: continue
        dxs.append(100*abs(pdi-mdi)/(pdi+mdi))
    if len(dxs) < p: return None
    adx = sum(dxs[:p]) / p
    for d in dxs[p:]: adx = (adx*(p-1) + d) / p
    return adx

def market_mode(c, h, l, price):
    atr = calc_atr(c, h, l, ATR_PERIOD)
    if atr is None: return "trend", None, None, None
    atr_pct = atr / price
    e20, e50 = calc_ema(c, 20), calc_ema(c, 50)
    adx = calc_adx(c, h, l, ADX_PERIOD)
    if e20 and e50:
        mode = "sideways" if atr_pct < SIDEWAYS_THR and abs(e20-e50)/price < 0.02 else "trend"
    else:
        mode = "sideways" if atr_pct < SIDEWAYS_THR else "trend"
    if adx is not None and adx >= ADX_THR: mode = "trend"
    return mode, atr, round(atr_pct*100, 2), adx

def signal_sideways(c, v, price, rsi):
    mid, up, lo = calc_bollinger(c, BB_PERIOD, BB_STD)
    if mid is None: return None
    vol_ok = v[-1] > (sum(v[-20:-1]) / 19) * 1.3 if len(v) >= 20 else True
    prev = c[-2]
    if prev <= lo and price > lo and rsi < 55 and vol_ok: return "Buy"
    if prev >= up and price < up and rsi > 45 and vol_ok: return "Sell"
    return None

def adaptive_usdt(atr_pct):
    # ВНИМАНИЕ: в manager.py сюда приходит atr_pct в ПРОЦЕНТАХ (напр. 1.2),
    # поэтому vol_mult почти всегда крошечный → размер = MIN_POSITION_USDT.
    # Воспроизводим как есть, чтобы бэктест совпадал с живым ботом.
    a = atr_pct if atr_pct and atr_pct > 0 else 0.02
    return max(min(QTY_USDT_BASE / (1 + a*10), MAX_POS), MIN_POS)

# ── данные ──────────────────────────────────────────────────────
def fetch_klines(symbol, days):
    step_ms = int(INTERVAL) * 60_000
    end = int(time.time()*1000)
    start = end - days*86_400_000 - WINDOW*step_ms
    out = {}
    while end > start:
        r = requests.get(f"{BASE}/v5/market/kline", timeout=15, params=dict(
            category="linear", symbol=symbol, interval=INTERVAL, end=end, limit=1000))
        r.raise_for_status()
        rows = r.json()["result"]["list"]
        if not rows: break
        for x in rows: out[int(x[0])] = x
        end = int(rows[-1][0]) - 1
        time.sleep(0.12)
    rows = [out[k] for k in sorted(out) if k >= start]
    if rows and int(rows[-1][0]) + step_ms > time.time()*1000:
        rows = rows[:-1]                       # выкинуть незакрытую свечу
    return ([int(x[0]) for x in rows], [float(x[2]) for x in rows],
            [float(x[3]) for x in rows], [float(x[4]) for x in rows],
            [float(x[5]) for x in rows])

# ── симуляция ──────────────────────────────────────────────────
def run(symbol, ts, H, L, C, V):
    trades, pos, cooldown_until = [], None, 0
    step_ms = int(INTERVAL) * 60_000
    for i in range(WINDOW, len(C)):
        # 1) проверяем TP/SL открытой позиции на этой свече (SL первым — консервативно)
        if pos:
            hit = None
            if pos["side"] == "Buy":
                if L[i] <= pos["sl"]: hit = ("SL", pos["sl"])
                elif H[i] >= pos["tp"]: hit = ("TP", pos["tp"])
            else:
                if H[i] >= pos["sl"]: hit = ("SL", pos["sl"])
                elif L[i] <= pos["tp"]: hit = ("TP", pos["tp"])
            if hit:
                res, px = hit
                d = 1 if pos["side"] == "Buy" else -1
                gross = d * (px - pos["entry"]) * pos["qty"]
                fees = (pos["entry"] + px) * pos["qty"] * FEE
                trades.append(dict(symbol=symbol, side=pos["side"], res=res,
                    entry=pos["entry"], exit=px, pnl=gross - fees, fees=fees,
                    t_open=pos["t"], t_close=ts[i], bars=i - pos["i"]))
                if res == "SL": cooldown_until = ts[i] + step_ms + COOLDOWN_MIN*60_000
                pos = None
                continue
        # 2) сигнал по закрытой свече i
        c, h, l, v = C[i-WINDOW+1:i+1], H[i-WINDOW+1:i+1], L[i-WINDOW+1:i+1], V[i-WINDOW+1:i+1]
        price = c[-1]
        mode, atr, atr_pct, _ = market_mode(c, h, l, price)
        if mode != "sideways": continue
        sig = signal_sideways(c, v, price, calc_rsi(c, RSI_PERIOD))
        if not sig: continue
        if pos:
            # противоположный сигнал в плюсе → закрыть по рынку (как в manager.py)
            d = 1 if pos["side"] == "Buy" else -1
            if sig != pos["side"] and d*(price - pos["entry"]) > 0:
                gross = d*(price - pos["entry"])*pos["qty"]
                fees = (pos["entry"] + price)*pos["qty"]*FEE
                trades.append(dict(symbol=symbol, side=pos["side"], res="signal",
                    entry=pos["entry"], exit=price, pnl=gross-fees, fees=fees,
                    t_open=pos["t"], t_close=ts[i], bars=i-pos["i"]))
                pos = None
            else:
                continue
        if ts[i] < cooldown_until: continue
        min_move = price * 0.005
        tp_m = max(atr*ATR_TP_MULT, min_move*2) if atr else price*0.03
        sl_m = max(atr*ATR_SL_MULT, min_move)   if atr else price*0.015
        d = 1 if sig == "Buy" else -1
        usdt = adaptive_usdt(atr_pct)
        pos = dict(side=sig, entry=price, qty=usdt/price, usdt=usdt,
                   tp=price + d*tp_m, sl=price - d*sl_m, t=ts[i], i=i)
    return trades

def report(trades, title):
    n = len(trades)
    if not n:
        print(f"{title}: сделок нет"); return
    w = [t for t in trades if t["pnl"] > 0]
    pnl = sum(t["pnl"] for t in trades)
    gp = sum(t["pnl"] for t in w); gl = -sum(t["pnl"] for t in trades if t["pnl"] <= 0)
    eq = peak = dd = 0.0
    for t in sorted(trades, key=lambda x: x["t_close"]):
        eq += t["pnl"]; peak = max(peak, eq); dd = min(dd, eq - peak)
    by = {r: sum(1 for t in trades if t["res"] == r) for r in ("TP", "SL", "signal")}
    print(f"{title}: сделок {n} | winrate {len(w)/n*100:.1f}% | PnL {pnl:+.2f}$ | "
          f"PF {gp/gl if gl else float('inf'):.2f} | maxDD {dd:.2f}$ | "
          f"комиссии {sum(t['fees'] for t in trades):.2f}$ | TP/SL/сигнал {by['TP']}/{by['SL']}/{by['signal']}")

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("symbols", nargs="*")
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--top", type=int, default=5, help="сколько монет брать из сканера")
    a = ap.parse_args()

    syms = a.symbols
    if not syms:
        from scanner import scan
        syms = [x["symbol"] for x in scan()][:a.top]
    print(f"Параметры: TF {INTERVAL}m, BB{BB_PERIOD}/{BB_STD}σ, ATR TP×{ATR_TP_MULT} SL×{ATR_SL_MULT}, "
          f"sideways<{SIDEWAYS_THR*100:.1f}%, ADX<{ADX_THR}, кулдаун {COOLDOWN_MIN:.0f}м, комиссия {FEE*100:.3f}%\n")
    allt = []
    for s in syms:
        try:
            data = fetch_klines(s, a.days)
        except Exception as e:
            print(f"{s}: ошибка загрузки — {e}"); continue
        t = run(s, *data); allt += t
        report(t, f"{s:<14}")
    print(); report(allt, f"ИТОГО за {a.days} дн")
    os.makedirs("logs", exist_ok=True)
    out = f"logs/backtest_{datetime.now(timezone.utc):%Y%m%d_%H%M}.json"
    json.dump(allt, open(out, "w"), indent=1)
    print(f"Сделки сохранены: {out}")

if __name__ == "__main__":
    main()
