#!/usr/bin/env python3
"""
scan_universe.py — проверка стратегии пробоя на широком рынке Bybit без «выбора победителей».

Вопрос: преимущество, найденное на 8 мейджорах, общее для рынка или нам повезло с монетами?

Правила отбора (заданы ЗАРАНЕЕ, не зависят от результатов стратегии):
  1. Все бессрочные USDT-контракты Bybit со статусом Trading.
  2. История начинается раньше, чем нужно для теста (иначе нечего проверять).
  3. Ранжирование по среднему дневному обороту за 30 дней ДО начала теста —
     то, что трейдер реально знал бы в тот момент. Берём топ-N.
  4. Проскальзывание по ликвидности на тот же момент: ≥ $500M/день → 0.02%, иначе 0.05%.
Стратегия и её параметры — из backtest_breakout.py без изменений (4h, канал 30, лимит 3 позиции).

Ограничение, которое не убрать: API Bybit отдаёт только монеты, торгующиеся сегодня.
Делистнутые проекты в выборку не попадают — это немного приукрашивает результат.

Запуск на VPS:
    venv/bin/python scan_universe.py --days 1095 --top 20
"""
import os, sys, time, json, argparse
from collections import defaultdict
from datetime import datetime, timezone

os.environ.setdefault("BO_INTERVAL", "240")
os.environ.setdefault("BO_DONCH", "30")
os.environ.setdefault("BO_SLIPPAGE", "0.0002")
import requests
import backtest_breakout as bb

DAY = 86_400_000
LIQUID_USD = 500e6           # порог «высокой ликвидности» для проскальзывания
SLIP_LIQUID, SLIP_OTHER = 0.0002, 0.0005

def _paged(path, **params):
    out, cursor = [], ""
    while True:
        r = requests.get(bb.BASE + path, params=dict(params, cursor=cursor) if cursor else params, timeout=15)
        r.raise_for_status()
        res = r.json()["result"]
        out += res.get("list", [])
        cursor = res.get("nextPageCursor") or ""
        if not cursor: return out
        time.sleep(0.1)

def instruments():
    return _paged("/v5/market/instruments-info", category="linear", limit=1000)

def pre_turnover(symbol, start_eval):
    """Средний дневной оборот (USD) за 30 дней до начала теста — по закрытым свечам."""
    rows = bb._get("/v5/market/kline", category="linear", symbol=symbol,
                   interval=bb.INTERVAL, end=start_eval - 1, limit=1000)
    rows = [x for x in rows if start_eval - 30 * DAY <= int(x[0]) < start_eval]
    if len(rows) < 30 * DAY // bb.STEP_MS * 0.9:     # пропуски — монета ещё не торговалась нормально
        return None
    return sum(float(x[6]) for x in rows) / 30

def report(allt, days, start_eval, per_coin):
    print("\n" + bb.line("ВСЕ СИГНАЛЫ", bb.stats(allt)))
    allt = bb.portfolio_cap(allt, bb.MAX_SAME_DIR) if bb.MAX_SAME_DIR > 0 else allt
    allt.sort(key=lambda x: x["t_close"])
    s = bb.stats(allt)
    print(f"(лимит портфеля: не больше {bb.MAX_SAME_DIR} позиций в одну сторону)")
    print(bb.line("ИТОГО", s))
    for rp in (0.005, 0.01):
        print(f"  maxDD депозита при риске {rp*100:.1f}% на сделку: {bb.dd_pct(allt, rp):.1f}%")

    mid = start_eval + days * DAY // 2
    h1 = bb.stats([t for t in allt if t["t_open"] < mid])
    h2 = bb.stats([t for t in allt if t["t_open"] >= mid])
    print(bb.line("1-я половина", h1)); print(bb.line("2-я половина", h2))

    by_y, by_m, by_side = defaultdict(list), defaultdict(list), defaultdict(list)
    for t in allt:
        dt = datetime.fromtimestamp(t["t_close"] / 1000, timezone.utc)
        by_y[dt.year].append(t); by_m[dt.strftime("%Y-%m")].append(t); by_side[t["side"]].append(t)
    print()
    for y, v in sorted(by_y.items()): print(bb.line(f"  {y} год", bb.stats(v)))
    for sd, v in sorted(by_side.items()): print(bb.line(f"  {'лонги' if sd == 'Buy' else 'шорты'}", bb.stats(v)))
    pos_m = sum(1 for v in by_m.values() if sum(x["R"] for x in v) > 0)

    # ширина преимущества: у скольких монет оно есть по отдельности
    pfs = sorted(c["pf"] for c in per_coin if c)
    if pfs:
        plus = sum(1 for c in per_coin if c and c["R"] > 0)
        print(f"\nШирина: в плюсе {plus} из {len(pfs)} монет, медианный PF {pfs[len(pfs)//2]:.2f}")

    checks = [
        ("сделок ≥ 100",                 s["n"] >= 100),
        ("PF после издержек ≥ 1.3",      s["pf"] >= 1.3),
        ("обе половины периода в плюсе", bool(h1 and h2 and h1["R"] > 0 and h2["R"] > 0)),
        ("плюсовых месяцев ≥ 60%",       pos_m / len(by_m) >= 0.6),
        ("maxDD ≤ 15R",                  s["dd"] >= -15),
    ]
    print("\nКритерии допуска:")
    for name, ok in checks: print(f"  {'✅' if ok else '❌'} {name}")
    print("→ " + ("ВСЕ пройдены." if all(ok for _, ok in checks) else "Не пройдены — на реальные деньги не переносим."))
    return allt

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=1095)
    ap.add_argument("--top", type=int, default=20)
    a = ap.parse_args()

    now = int(time.time() * 1000)
    start_eval = now - a.days * DAY
    need_from = start_eval - bb.WARMUP * bb.STEP_MS - 30 * DAY

    inst = instruments()
    perps = [x for x in inst if x.get("contractType") == "LinearPerpetual"
             and x.get("status") == "Trading" and x.get("quoteCoin") == "USDT"]
    old = [x["symbol"] for x in perps if int(x.get("launchTime") or now) <= need_from]
    print(f"Бессрочных USDT-контрактов: {len(perps)} | с историей с {datetime.fromtimestamp(need_from/1000, timezone.utc):%Y-%m-%d}: {len(old)}")
    print(f"Считаю оборот за 30 дней до {datetime.fromtimestamp(start_eval/1000, timezone.utc):%Y-%m-%d} …")

    ranked = []
    for i, s in enumerate(old, 1):
        try:
            t = pre_turnover(s, start_eval)
        except Exception as e:
            t = None
        if t: ranked.append((t, s))
        if i % 25 == 0: print(f"  {i}/{len(old)}", flush=True)
        time.sleep(0.05)
    ranked.sort(reverse=True)
    universe = ranked[:a.top]
    print(f"\nВыборка (топ-{a.top} по обороту на старте теста, НЕ по прибыльности):")
    print("  " + ", ".join(f"{s} ${t/1e6:,.0f}M" for t, s in universe))

    print(f"\nСтратегия: пробой {bb.DONCH} × {bb.INTERVAL}m, EMA{bb.EMA_FAST}/{bb.EMA_SLOW}, объём ×{bb.VOL_MULT}, "
          f"стоп {bb.STOP_ATR}ATR, трейлинг {bb.TRAIL_ATR}ATR, taker {bb.TAKER*100:.3f}% | {a.days} дн\n")
    allt, per_coin = [], []
    for turnover, s in universe:
        bb.SLIP = SLIP_LIQUID if turnover >= LIQUID_USD else SLIP_OTHER
        try:
            ts, O, H, L, C, V = bb.fetch_klines(s, a.days)
            if len(C) < bb.WARMUP + 50:
                print(f"{s:<16} мало истории — пропуск"); continue
            fund = bb.fetch_funding(s, ts[0])
        except Exception as e:
            print(f"{s:<16} ошибка загрузки — {e}"); continue
        tr = bb.run(s, ts, O, H, L, C, V, fund, start_eval)
        st = bb.stats(tr); per_coin.append(st); allt += tr
        print(bb.line(s, st) + f" | слипп. {bb.SLIP*100:.2f}%")

    if not allt:
        print("\nСделок нет."); return
    allt = report(allt, a.days, start_eval, per_coin)

    os.makedirs("logs", exist_ok=True)
    out = f"logs/scan_universe_{datetime.now(timezone.utc):%Y%m%d_%H%M}.json"
    json.dump(dict(universe=[s for _, s in universe], trades=allt), open(out, "w"), indent=1)
    print(f"\nСделки сохранены: {out}")

if __name__ == "__main__":
    main()
