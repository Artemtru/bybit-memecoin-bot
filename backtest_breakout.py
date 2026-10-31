#!/usr/bin/env python3
"""
backtest_breakout.py — бэктест трендовой стратегии «пробой канала» для мемкоинов.

Идея (выводы из бэктеста старой BB-стратегии, 31.10.2026):
  * на 5m средняя сделка (~0.07%) меньше издержек (~0.11%) → берём 1h, стопы шире;
  * у мемкоинов выход за диапазон чаще продолжение, чем откат → торгуем пробой
    по тренду, а не возврат к средней;
  * фиксированный TP режет прибыль → выходим трейлинг-стопом.

Правила (зафиксированы ЗАРАНЕЕ, не подбираются под историю):
  Long : close > максимум предыдущих DONCH баров  И  EMA50 > EMA200  И  объём > VOL_MULT × средний(20)
  Short: зеркально.
  Вход : по рынку на открытии следующего бара (taker + проскальзывание).
  Стоп : вход ∓ STOP_ATR × ATR(14).
  Выход: трейлинг «люстра» — экстремум с момента входа ∓ TRAIL_ATR × ATR; стоп только подтягивается.
         Гэп через стоп → исполнение по цене открытия (хуже стопа).
  Размер: фиксированный риск 1R = RISK_USDT на сделку (qty = риск / расстояние до стопа).
  Funding: реальная история ставок Bybit, списывается/начисляется пока позиция открыта.

Результат в R (1R = риск на сделку). При RISK_USDT=1 доллары = R.

Запуск на VPS (ключи не нужны, ордера не ставятся):
    venv/bin/python backtest_breakout.py --days 180
    venv/bin/python backtest_breakout.py DOGEUSDT WIFUSDT --days 365
"""
import argparse, os, time, json
from collections import defaultdict
from datetime import datetime, timezone

import requests

E = lambda k, d: float(os.getenv(k, d))
INTERVAL   = os.getenv("BO_INTERVAL", "60")       # минуты
DONCH      = int(E("BO_DONCH", 48))               # 48 × 1h = 2 суток
EMA_FAST   = int(E("BO_EMA_FAST", 50))
EMA_SLOW   = int(E("BO_EMA_SLOW", 200))
VOL_MULT   = E("BO_VOL_MULT", 1.5)
ATR_P      = int(E("BO_ATR", 14))
STOP_ATR   = E("BO_STOP_ATR", 2.0)
TRAIL_ATR  = E("BO_TRAIL_ATR", 3.0)
TAKER      = E("BO_TAKER_FEE", 0.00055)
SLIP       = E("BO_SLIPPAGE", 0.0005)             # 0.05% на сторону — мемкоины, рыночные ордера
RISK_USDT  = E("BO_RISK_USDT", 1.0)
BASE       = "https://api.bybit.com"
STEP_MS    = int(INTERVAL) * 60_000
WARMUP     = max(EMA_SLOW * 3, DONCH + 1, 60)

# Фиксированный список, а не сканер: сканер выбирает монеты, которые выросли
# СЕЙЧАС, — это подглядывание в будущее и завышает результат.
DEFAULT_SYMBOLS = ["DOGEUSDT", "1000PEPEUSDT", "WIFUSDT", "POPCATUSDT", "1000BONKUSDT",
                   "PNUTUSDT", "MEWUSDT", "BRETTUSDT", "1000FLOKIUSDT", "SHIB1000USDT"]

# ── данные ──────────────────────────────────────────────────────
def _get(path, **params):
    for attempt in range(4):
        try:
            r = requests.get(BASE + path, params=params, timeout=15)
            r.raise_for_status()
            j = r.json()
            if j.get("retCode") != 0:
                raise RuntimeError(j.get("retMsg"))
            return j["result"]["list"]
        except Exception:
            if attempt == 3: raise
            time.sleep(1 + attempt)

def fetch_klines(symbol, days):
    end = int(time.time() * 1000)
    start = end - days * 86_400_000 - WARMUP * STEP_MS
    out = {}
    while end > start:
        rows = _get("/v5/market/kline", category="linear", symbol=symbol,
                    interval=INTERVAL, end=end, limit=1000)
        if not rows: break
        for x in rows: out[int(x[0])] = x
        end = int(rows[-1][0]) - 1
        time.sleep(0.1)
    rows = [out[k] for k in sorted(out) if k >= start]
    if rows and int(rows[-1][0]) + STEP_MS > time.time() * 1000:
        rows = rows[:-1]                                   # незакрытая свеча
    cols = list(zip(*[(int(x[0]), float(x[1]), float(x[2]), float(x[3]), float(x[4]), float(x[5]))
                      for x in rows])) if rows else [[]] * 6
    return [list(c) for c in cols]                         # ts, O, H, L, C, V

def fetch_funding(symbol, start_ms):
    end = int(time.time() * 1000)
    out = {}
    while end > start_ms:
        rows = _get("/v5/market/funding/history", category="linear", symbol=symbol,
                    endTime=end, limit=200)
        if not rows: break
        for x in rows:
            t = int(x["fundingRateTimestamp"])
            out[t // STEP_MS * STEP_MS] = float(x["fundingRate"])
        new_end = min(int(x["fundingRateTimestamp"]) for x in rows) - 1
        if new_end >= end: break
        end = new_end
        time.sleep(0.1)
    return out

# ── индикаторы (массивы, значение на закрытии бара i) ─────────────
def ema(a, p):
    out, k, e = [None] * len(a), 2 / (p + 1), None
    for i, x in enumerate(a):
        if i == p - 1: e = sum(a[:p]) / p
        elif i >= p:   e = x * k + e * (1 - k)
        out[i] = e
    return out

def atr(H, L, C, p):
    out, a = [None] * len(C), None
    for i in range(1, len(C)):
        tr = max(H[i] - L[i], abs(H[i] - C[i-1]), abs(L[i] - C[i-1]))
        if i < p:    a = (a or 0) + tr
        elif i == p: a = (a + tr) / p
        else:        a = (a * (p - 1) + tr) / p
        if i >= p: out[i] = a
    return out

# ── симуляция ──────────────────────────────────────────────────
def run(symbol, ts, O, H, L, C, V, funding, start_eval):
    n = len(C)
    ef, es, at = ema(C, EMA_FAST), ema(C, EMA_SLOW), atr(H, L, C, ATR_P)
    trades, pos = [], None

    def close(i, px, reason):
        d = pos["d"]
        exit_px = px * (1 - d * SLIP)
        gross = d * (exit_px - pos["entry"]) * pos["qty"]
        fees = (pos["entry"] + exit_px) * pos["qty"] * TAKER
        net = gross - fees + pos["funding"]
        trades.append(dict(symbol=symbol, side="Buy" if d == 1 else "Sell", reason=reason,
            entry=pos["entry"], exit=exit_px, t_open=ts[pos["i"]], t_close=ts[i],
            hours=(ts[i] - ts[pos["i"]]) / 3_600_000, fees=fees, funding=pos["funding"],
            R=net / RISK_USDT, R_gross=gross / RISK_USDT))

    for i in range(WARMUP, n):
        # 1) ведём открытую позицию на баре i
        if pos and i >= pos["i"]:
            d = pos["d"]
            if i > pos["i"] and ts[i] in funding:          # расчёт funding в начале бара
                pos["funding"] -= d * funding[ts[i]] * pos["qty"] * O[i]
            exited = False
            if i > pos["i"] and (O[i] - pos["stop"]) * d <= 0:
                close(i, O[i], "gap"); exited = True       # гэп через стоп
            elif (L[i] if d == 1 else H[i]) * d <= pos["stop"] * d:
                close(i, pos["stop"], "stop" if not pos["trailed"] else "trail"); exited = True
            if exited:
                pos = None
            else:                                          # подтягиваем трейлинг по закрытию
                pos["ext"] = max(pos["ext"], H[i]) if d == 1 else min(pos["ext"], L[i])
                trail = pos["ext"] - d * TRAIL_ATR * at[i]
                if (trail - pos["stop"]) * d > 0:
                    pos["stop"], pos["trailed"] = trail, True

        # 2) сигнал на закрытии бара i → вход на открытии i+1
        if pos or i + 1 >= n or ts[i + 1] < start_eval or at[i] is None or es[i] is None:
            continue
        hi = max(H[i - DONCH:i]); lo = min(L[i - DONCH:i])
        vol_ok = V[i] > VOL_MULT * (sum(V[i - 20:i]) / 20)
        d = 0
        if C[i] > hi and ef[i] > es[i] and vol_ok: d = 1
        if C[i] < lo and ef[i] < es[i] and vol_ok: d = -1
        if not d: continue
        entry = O[i + 1] * (1 + d * SLIP)
        stop = entry - d * STOP_ATR * at[i]
        dist = abs(entry - stop)
        if dist <= 0: continue
        pos = dict(d=d, i=i + 1, entry=entry, stop=stop, qty=RISK_USDT / dist,
                   ext=entry, trailed=False, funding=0.0)
    return trades

# ── отчёт ──────────────────────────────────────────────────────
def stats(tr):
    if not tr: return None
    n = len(tr); w = [t for t in tr if t["R"] > 0]
    gp = sum(t["R"] for t in w); gl = -sum(t["R"] for t in tr if t["R"] <= 0)
    eq = peak = dd = 0.0
    for t in sorted(tr, key=lambda x: x["t_close"]):
        eq += t["R"]; peak = max(peak, eq); dd = min(dd, eq - peak)
    return dict(n=n, win=len(w) / n * 100, R=sum(t["R"] for t in tr),
                Rg=sum(t["R_gross"] for t in tr), avg=sum(t["R"] for t in tr) / n,
                pf=gp / gl if gl else float("inf"), dd=dd,
                hours=sum(t["hours"] for t in tr) / n,
                fund=sum(t["funding"] for t in tr) / RISK_USDT)

def line(title, s):
    if not s: return f"{title:<16} сделок нет"
    return (f"{title:<16} сделок {s['n']:>4} | win {s['win']:5.1f}% | итог {s['R']:+7.2f}R "
            f"(до издержек {s['Rg']:+.2f}R) | ср. {s['avg']:+.3f}R | PF {s['pf']:.2f} | "
            f"maxDD {s['dd']:.2f}R | держим {s['hours']:.0f}ч | funding {s['fund']:+.2f}R")

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("symbols", nargs="*")
    ap.add_argument("--days", type=int, default=180)
    a = ap.parse_args()
    syms = a.symbols or DEFAULT_SYMBOLS
    start_eval = int(time.time() * 1000) - a.days * 86_400_000

    print(f"Пробой {DONCH} баров × {INTERVAL}m | тренд EMA{EMA_FAST}/{EMA_SLOW} | объём ×{VOL_MULT} | "
          f"стоп {STOP_ATR}ATR, трейлинг {TRAIL_ATR}ATR | taker {TAKER*100:.3f}% + слипп. {SLIP*100:.2f}% "
          f"| {a.days} дн\n")
    allt = []
    for s in syms:
        try:
            ts, O, H, L, C, V = fetch_klines(s, a.days)
            if len(C) < WARMUP + 50:
                print(f"{s:<16} мало истории ({len(C)} баров) — пропуск"); continue
            fund = fetch_funding(s, ts[0])
        except Exception as e:
            print(f"{s:<16} ошибка загрузки — {e}"); continue
        t = run(s, ts, O, H, L, C, V, fund, start_eval)
        allt += t
        print(line(s, stats(t)))

    if not allt:
        print("\nСделок нет."); return
    allt.sort(key=lambda x: x["t_close"])
    print("\n" + line("ИТОГО", stats(allt)))

    # устойчивость: первая и вторая половина периода отдельно
    mid = start_eval + a.days * 86_400_000 // 2
    print(line("1-я половина", stats([t for t in allt if t["t_open"] < mid])))
    print(line("2-я половина", stats([t for t in allt if t["t_open"] >= mid])))

    # по месяцам
    by_m = defaultdict(list)
    for t in allt:
        by_m[datetime.fromtimestamp(t["t_close"] / 1000, timezone.utc).strftime("%Y-%m")].append(t)
    print("\nПо месяцам (R):  " + "  ".join(f"{m}: {sum(x['R'] for x in v):+.1f}"
                                          for m, v in sorted(by_m.items())))
    pos_m = sum(1 for v in by_m.values() if sum(x["R"] for x in v) > 0)

    # критерии допуска к реальным деньгам — заданы до получения результатов
    s = stats(allt)
    h1 = stats([t for t in allt if t["t_open"] < mid])
    h2 = stats([t for t in allt if t["t_open"] >= mid])
    checks = [
        ("сделок ≥ 100",                  s["n"] >= 100),
        ("PF после издержек ≥ 1.3",       s["pf"] >= 1.3),
        ("обе половины периода в плюсе",  bool(h1 and h2 and h1["R"] > 0 and h2["R"] > 0)),
        ("плюсовых месяцев ≥ 60%",        pos_m / len(by_m) >= 0.6),
        ("maxDD ≤ 15R",                   s["dd"] >= -15),
    ]
    print("\nКритерии допуска:")
    for name, ok in checks:
        print(f"  {'✅' if ok else '❌'} {name}")
    print("→ " + ("ВСЕ пройдены — можно обсуждать перенос в бота."
                  if all(ok for _, ok in checks) else "Не пройдены — на реальные деньги не переносим."))

    os.makedirs("logs", exist_ok=True)
    out = f"logs/backtest_breakout_{datetime.now(timezone.utc):%Y%m%d_%H%M}.json"
    json.dump(allt, open(out, "w"), indent=1)
    print(f"\nСделки сохранены: {out}")

if __name__ == "__main__":
    main()
