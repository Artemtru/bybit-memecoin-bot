#!/usr/bin/env python3
"""
backtest.py — бэктест боковой стратегии (BB + RSI + объём + режим ATR/EMA/ADX) на истории Bybit.

Индикаторы и сигнал берутся НАПРЯМУЮ из manager.py (через ast), поэтому логика совпадает с боевой.
Данные: публичный API Bybit (ключи не нужны), 5-минутные свечи.

Запуск на сервере:
    cd /root/bybit-memecoin-bot
    venv/bin/python backtest.py --days 30
    venv/bin/python backtest.py --days 30 --fast        # меньше сетка, быстрее
    venv/bin/python backtest.py --synthetic --fast      # проверка самого скрипта без сети
Результат: таблица в консоли + backtest_results.md + backtest_results.json.

Как читать результат:
  • данные делятся на train (первые 2/3) и test (последняя 1/3). Параметры, выигравшие на train,
    но проигравшие на test, — подгонка под шум, а не edge;
  • если положительных на test мало — устойчивого edge у стратегии нет, параметры крутить бессмысленно.

Допущения (результат нужно читать с их учётом):
  • вход по закрытию свечи-сигнала, выход по TP/SL внутри следующих свечей;
    если в одной свече задеты и TP, и SL — считается SL (консервативно);
  • комиссия taker 0.055% за сторону + проскальзывание 0.02% за сторону;
  • одна позиция на символ, после стопа кулдаун 20 минут;
    закрытие по обратному сигналу, дневной лимит и «стоп-кран» не моделируются;
  • используются закрытые свечи, а бот смотрит на «живую»: реальные входы чуть раньше/позже.
"""
import argparse
import ast
import itertools
import json
import math
import os
import random
import sys
import time
from decimal import Decimal

WINDOW = 150            # как в боте: get_klines(limit=150)
CANDLE_MS = 5 * 60 * 1000
FEE_RATE = 0.00055      # taker, за сторону
COOLDOWN_MIN = 20       # STOP_COOLDOWN_MIN в боте

DEFAULT_SYMBOLS = [
    "1000PEPEUSDT", "TRUMPUSDT", "WIFUSDT", "1000BONKUSDT", "POPCATUSDT",
    "DOGEUSDT", "1000FLOKIUSDT", "NOTUSDT", "BRETTUSDT",
]

# Константы manager.py, на которые ссылаются индикаторные функции (значения = боевые дефолты)
MANAGER_DEFAULTS = {
    "ATR_PERIOD": 14, "ADX_PERIOD": 14,
    "SIDEWAYS_ATR_THRESHOLD": 0.015, "ADX_TREND_THRESHOLD": 25.0,
    "BB_PERIOD": 20, "BB_STD": 1.5,
    "SIDEWAYS_RSI_LONG_MAX": 55.0, "SIDEWAYS_RSI_SHORT_MIN": 45.0,
    "RSI_BUY_CURRENT": 42.0, "RSI_SELL_CURRENT": 58.0,
}
NEEDED_FUNCS = {
    "calc_rsi", "calc_bollinger", "calc_atr", "calc_ema", "calc_adx",
    "detect_market_mode", "signal_sideways",
}


# ═══════════════════════════════════════════════════
#   ЗАГРУЗКА ЛОГИКИ ИЗ manager.py
# ═══════════════════════════════════════════════════

def load_manager_funcs(path):
    src = open(path, encoding="utf-8").read()
    tree = ast.parse(src)
    ns = {"math": math, "Decimal": Decimal}
    ns.update(MANAGER_DEFAULTS)
    found = set()
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in NEEDED_FUNCS:
            mod = ast.Module(body=[node], type_ignores=[])
            exec(compile(mod, path, "exec"), ns)
            found.add(node.name)
    missing = NEEDED_FUNCS - found
    if missing:
        sys.exit(f"В {path} не найдены функции: {sorted(missing)}")
    return ns


# ═══════════════════════════════════════════════════
#   ДАННЫЕ
# ═══════════════════════════════════════════════════

def fetch_klines(symbol, days, cache_dir):
    """5-минутные свечи с публичного API Bybit (v5), с кэшем на 6 часов."""
    os.makedirs(cache_dir, exist_ok=True)
    path = os.path.join(cache_dir, f"{symbol}_5m_{days}d.json")
    if os.path.exists(path) and time.time() - os.path.getmtime(path) < 6 * 3600:
        return json.load(open(path))

    import requests
    end = int(time.time() * 1000)
    since = end - days * 86400 * 1000
    rows = {}
    while end > since:
        r = requests.get(
            "https://api.bybit.com/v5/market/kline",
            params={"category": "linear", "symbol": symbol, "interval": "5",
                    "limit": 1000, "end": end},
            timeout=20,
        )
        r.raise_for_status()
        lst = r.json().get("result", {}).get("list", [])
        if not lst:
            break
        for c in lst:
            rows[int(c[0])] = (float(c[2]), float(c[3]), float(c[4]), float(c[5]))
        oldest = min(int(c[0]) for c in lst)
        if oldest >= end:
            break
        end = oldest - 1
        time.sleep(0.12)

    ts = sorted(t for t in rows if t >= since)
    data = {
        "ts":    ts,
        "high":  [rows[t][0] for t in ts],
        "low":   [rows[t][1] for t in ts],
        "close": [rows[t][2] for t in ts],
        "vol":   [rows[t][3] for t in ts],
    }
    json.dump(data, open(path, "w"))
    return data


def synthetic_klines(days, seed):
    """Случайные свечи для проверки скрипта без сети (НЕ для выводов о стратегии)."""
    rnd = random.Random(seed)
    n = days * 288
    ts0 = int(time.time() * 1000) - n * CANDLE_MS
    price, vol_state = 1.0, 0.002
    d = {"ts": [], "high": [], "low": [], "close": [], "vol": []}
    for k in range(n):
        vol_state = max(0.0008, min(0.006, vol_state * 0.98 + abs(rnd.gauss(0, 0.0004))))
        ret = rnd.gauss(0, vol_state) - 0.05 * math.log(price)
        o = price
        price = o * math.exp(ret)
        hi = max(o, price) * (1 + abs(rnd.gauss(0, vol_state / 2)))
        lo = min(o, price) * (1 - abs(rnd.gauss(0, vol_state / 2)))
        d["ts"].append(ts0 + k * CANDLE_MS)
        d["high"].append(hi)
        d["low"].append(lo)
        d["close"].append(price)
        d["vol"].append(rnd.lognormvariate(0, 0.5))
    return d


# ═══════════════════════════════════════════════════
#   СИМУЛЯЦИЯ
# ═══════════════════════════════════════════════════

def precompute(ns, d):
    """Признаки, не зависящие от перебираемых параметров: (режим, ATR, RSI) для каждой свечи."""
    closes, highs, lows = d["close"], d["high"], d["low"]
    feats = [None] * len(closes)
    for i in range(WINDOW - 1, len(closes)):
        a = i - WINDOW + 1
        c, h, l = closes[a:i + 1], highs[a:i + 1], lows[a:i + 1]
        mode, atr, _atr_pct, _adx = ns["detect_market_mode"](c, h, l, c[-1])
        feats[i] = (mode, atr, ns["calc_rsi"](c, 14))
    return feats


def calc_trade_pnl(is_long, entry_px, exit_px, notional, fee):
    qty = notional / entry_px
    gross = qty * (exit_px - entry_px) if is_long else qty * (entry_px - exit_px)
    return gross - fee * (qty * entry_px + qty * exit_px)


def simulate(ns, d, feats, p, slip, fee):
    ns["BB_STD"] = p["bb_std"]
    ns["SIDEWAYS_RSI_LONG_MAX"] = p["rsi_long_max"]
    ns["SIDEWAYS_RSI_SHORT_MIN"] = p["rsi_short_min"]
    ts, closes, highs, lows, vols = d["ts"], d["close"], d["high"], d["low"], d["vol"]
    n = len(closes)
    cooldown_candles = int(COOLDOWN_MIN * 60_000 / CANDLE_MS)
    trades = []
    i = WINDOW - 1
    cooldown_until = -1
    while i < n - 1:
        f = feats[i]
        if f is None or f[0] != "sideways" or i < cooldown_until:
            i += 1
            continue
        a = i - WINDOW + 1
        sig, _, _ = ns["signal_sideways"](
            closes[a:i + 1], highs[a:i + 1], lows[a:i + 1], closes[i], f[2], vols[a:i + 1]
        )
        if not sig:
            i += 1
            continue

        price, atr = closes[i], f[1]
        min_move = price * p["min_sl_pct"]
        if atr and atr > 0:
            tp_move = max(atr * p["tp_mult"], min_move * 2)
            sl_move = max(atr * p["sl_mult"], min_move)
        else:
            tp_move, sl_move = price * 0.03, price * 0.015

        is_long = sig == "Buy"
        entry = price * (1 + slip) if is_long else price * (1 - slip)
        tp = price + tp_move if is_long else price - tp_move
        sl = price - sl_move if is_long else price + sl_move

        j, exit_px, kind = i + 1, None, None
        while j < n:
            hit_sl = lows[j] <= sl if is_long else highs[j] >= sl
            hit_tp = highs[j] >= tp if is_long else lows[j] <= tp
            if hit_sl:  # если задеты оба — консервативно считаем стоп
                exit_px = sl * (1 - slip) if is_long else sl * (1 + slip)
                kind = "SL"
                break
            if hit_tp:
                exit_px = tp * (1 - slip) if is_long else tp * (1 + slip)
                kind = "TP"
                break
            j += 1
        if exit_px is None:   # позиция не закрылась до конца данных — не считаем
            break

        trades.append({
            "t": ts[i], "kind": kind, "side": sig,
            "pnl": calc_trade_pnl(is_long, entry, exit_px, p["notional"], fee),
        })
        i = j + 1
        if kind == "SL":
            cooldown_until = j + cooldown_candles
    return trades


def stats(trades):
    n = len(trades)
    if n == 0:
        return {"n": 0, "wr": 0.0, "pnl": 0.0, "pf": 0.0, "aw": 0.0, "al": 0.0}
    wins = [t["pnl"] for t in trades if t["pnl"] > 0]
    losses = [t["pnl"] for t in trades if t["pnl"] <= 0]
    gw, gl = sum(wins), -sum(losses)
    return {
        "n": n, "wr": len(wins) / n * 100, "pnl": sum(t["pnl"] for t in trades),
        "pf": (gw / gl) if gl > 0 else float("inf"),
        "aw": gw / len(wins) if wins else 0.0,
        "al": -gl / len(losses) if losses else 0.0,
    }


# ═══════════════════════════════════════════════════
#   СЕТКА ПАРАМЕТРОВ
# ═══════════════════════════════════════════════════

def build_grid(fast):
    if fast:
        bb, rsi, tp, minsl = [1.5, 2.0, 3.0], [(55, 45), (40, 60)], [2.5, 3.5], [0.005]
    else:
        bb = [1.5, 2.0, 2.5, 3.0]
        rsi = [(55, 45), (45, 55), (40, 60), (35, 65)]   # (LONG при RSI<x, SHORT при RSI>y)
        tp = [2.5, 3.5]
        minsl = [0.004, 0.005, 0.0075]
    return [
        {"bb_std": b, "rsi_long_max": r[0], "rsi_short_min": r[1],
         "tp_mult": t, "sl_mult": 1.2, "min_sl_pct": m}
        for b, r, t, m in itertools.product(bb, rsi, tp, minsl)
    ]


def label(p):
    return (f"BB{p['bb_std']} RSI<{p['rsi_long_max']:g}/>{p['rsi_short_min']:g} "
            f"TP×{p['tp_mult']} SL≥{p['min_sl_pct']*100:.2f}%")


def fmt_pf(x):
    return "∞" if x == float("inf") else f"{x:.2f}"


# ═══════════════════════════════════════════════════
#   ГЛАВНАЯ ЧАСТЬ
# ═══════════════════════════════════════════════════

def run(ns, data, args):
    t_all = [t for d in data.values() for t in d["ts"]]
    t0, t1 = min(t_all), max(t_all)
    split = t0 + (t1 - t0) * 2 / 3

    print(f"Признаки (режим/ATR/RSI) по {len(data)} монетам...", flush=True)
    t_start = time.time()
    feats = {s: precompute(ns, d) for s, d in data.items()}
    print(f"  готово за {time.time() - t_start:.0f} c", flush=True)

    grid = build_grid(args.fast)
    print(f"Перебор {len(grid)} комбинаций параметров...", flush=True)
    rows = []
    for k, p in enumerate(grid, 1):
        p = dict(p, notional=args.notional)
        trades = []
        for s, d in data.items():
            trades += simulate(ns, d, feats[s], p, args.slip, args.fee)
        tr = [t for t in trades if t["t"] < split]
        te = [t for t in trades if t["t"] >= split]
        rows.append({"p": p, "all": stats(trades), "train": stats(tr), "test": stats(te)})
        if k % 10 == 0:
            print(f"  {k}/{len(grid)}", flush=True)
    return rows, split


def report(rows, data, args, split):
    days_total = (max(max(d["ts"]) for d in data.values()) -
                  min(min(d["ts"]) for d in data.values())) / 86_400_000
    out = []
    w = out.append
    w(f"# Бэктест боковой стратегии\n")
    w(f"Монет: {len(data)} ({', '.join(data)}). Период: {days_total:.0f} дн., "
      f"позиция ${args.notional:g}, комиссия {args.fee*100:.3f}%/сторона, проскальзывание {args.slip*100:.3f}%/сторона.")
    w(f"Train = первые 2/3 периода, test = последняя 1/3. Минимум сделок на train: {args.min_trades}.\n")

    base_old = next((r for r in rows if r["p"]["bb_std"] == 1.5 and r["p"]["rsi_long_max"] == 55
                     and r["p"]["tp_mult"] == 2.5 and r["p"]["min_sl_pct"] == 0.005), None)
    base_live = next((r for r in rows if r["p"]["bb_std"] == 3.0 and r["p"]["rsi_long_max"] == 55
                      and r["p"]["tp_mult"] == 3.5 and r["p"]["min_sl_pct"] == 0.005), None)

    def line(tag, r):
        a, tr, te = r["all"], r["train"], r["test"]
        return (f"| {tag} | {label(r['p'])} | {a['n']} | {a['wr']:.0f}% | {fmt_pf(a['pf'])} | "
                f"{a['pnl']:+.3f} | {tr['pnl']:+.3f} ({tr['n']}) | {te['pnl']:+.3f} ({te['n']}) |")

    header = ("| | параметры | сделок | WR | PF | PnL $ (всё) | train $ (n) | test $ (n) |\n"
              "|---|---|---|---|---|---|---|---|")
    w("## Контрольные точки")
    w(header)
    if base_old:
        w(line("до Phase 1 (BB1.5)", base_old))
    if base_live:
        w(line("живой конфиг (BB3.0)", base_live))

    ranked = [r for r in rows if r["train"]["n"] >= args.min_trades]
    ranked.sort(key=lambda r: r["train"]["pnl"], reverse=True)
    w("\n## Топ-10 по train (и что с ними на test)")
    w(header)
    for k, r in enumerate(ranked[:10], 1):
        w(line(str(k), r))

    pos_train = [r for r in ranked if r["train"]["pnl"] > 0]
    min_test = max(5, args.min_trades // 3)
    robust = [r for r in pos_train if r["test"]["n"] >= min_test and r["test"]["pnl"] > 0]
    avg = [r for r in ranked if r["all"]["n"] > 0]
    mean_pnl = sum(r["all"]["pnl"] for r in avg) / len(avg) if avg else 0.0
    best_all = max(ranked, key=lambda r: r["all"]["pnl"], default=None)

    w("\n## Вывод")
    w(f"- комбинаций с достаточным числом сделок: {len(ranked)}; "
      f"плюсовых на train: {len(pos_train)}; из них плюсовых и на test: {len(robust)}.")
    w(f"- средний PnL по всем комбинациям: {mean_pnl:+.3f} $ за период "
      f"(≈ {mean_pnl / max(days_total, 1) * 30:+.2f} $ в месяц на позиции ${args.notional:g}).")
    if best_all:
        a = best_all["all"]
        w(f"- лучшая комбинация за весь период: {label(best_all['p'])} → {a['pnl']:+.3f} $, "
          f"{a['n']} сделок, WR {a['wr']:.0f}%, ср.выигрыш {a['aw']:.3f}, ср.проигрыш {a['al']:.3f}.")
    if not pos_train or len(robust) < max(1, len(pos_train) // 2):
        w("- **Устойчивого edge не видно**: выигрыш на train не повторяется на test. "
          "Крутить параметры на боевом не нужно; пересматривать надо саму идею входа/выхода.")
    else:
        w("- Есть комбинации, плюсовые и на train, и на test. Это повод для проверки на другом "
          "периоде/монетах и только потом для малого боевого теста, но не гарантия прибыли.")
    w("- Помни про допущения (закрытые свечи, нет обратных сигналов/лимитов) и малую выборку: "
      "на нескольких десятках сделок погрешность винрейта ±10–15 п.п.")
    return "\n".join(out)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--symbols", nargs="+", default=DEFAULT_SYMBOLS)
    ap.add_argument("--notional", type=float, default=10.0, help="размер позиции, $")
    ap.add_argument("--fee", type=float, default=FEE_RATE)
    ap.add_argument("--slip", type=float, default=0.0002)
    ap.add_argument("--min-trades", type=int, default=20)
    ap.add_argument("--fast", action="store_true")
    ap.add_argument("--synthetic", action="store_true", help="случайные данные, без сети")
    ap.add_argument("--manager", default="manager.py")
    ap.add_argument("--cache", default="backtest_cache")
    args = ap.parse_args()

    ns = load_manager_funcs(args.manager)
    data = {}
    for k, s in enumerate(args.symbols):
        if args.synthetic:
            data[s] = synthetic_klines(args.days, seed=k + 1)
            continue
        print(f"Загрузка {s}...", flush=True)
        try:
            d = fetch_klines(s, args.days, args.cache)
        except Exception as e:
            print(f"  пропуск {s}: {e}")
            continue
        if len(d["close"]) < WINDOW + 50:
            print(f"  пропуск {s}: мало свечей ({len(d['close'])})")
            continue
        data[s] = d
    if not data:
        sys.exit("Нет данных ни по одной монете (сеть? символы?).")

    rows, split = run(ns, data, args)
    text = report(rows, data, args, split)
    print("\n" + text)
    open("backtest_results.md", "w", encoding="utf-8").write(text + "\n")
    json.dump(rows, open("backtest_results.json", "w"), ensure_ascii=False, indent=1, default=str)
    print("\nСохранено: backtest_results.md, backtest_results.json")


if __name__ == "__main__":
    main()
