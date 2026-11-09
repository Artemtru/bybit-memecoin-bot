"""
╔══════════════════════════════════════════════════════╗
║  FUNDING_BACKTEST.PY — Моделирование арбитража       ║
║  ставки финансирования (cash-and-carry)              ║
║                                                      ║
║  Позиция: ЛОНГ спот + ШОРТ перпетуал на ту же сумму. ║
║  Цена монеты не влияет на результат, доход —         ║
║  выплаты funding шортам (минус, если funding < 0).   ║
║                                                      ║
║  Только публичные данные Bybit, ордеров НЕТ.         ║
║  Комиссии: спот taker 0.1%, фьючерс taker 0.055%,    ║
║  проскальзывание 0.02% — на каждой ноге, вход+выход. ║
║  Капитал: 1× под спот + 0.5× маржа под шорт (2x).    ║
║  Проверка правил входа/выхода — вне выборки.         ║
╚══════════════════════════════════════════════════════╝

Запуск вручную:  python funding_backtest.py
Из Telegram:     /funding
"""

from __future__ import annotations

import itertools
import json
import os
import time
from datetime import datetime, timezone

import requests
from dotenv import load_dotenv

load_dotenv()

FUNDING_VERSION = "2026-11-09.1"     # смена версии → авто-прогон после деплоя
REPORT_FILE     = "logs/funding_report.json"
VERSION_FILE    = "logs/funding_version.txt"
BYBIT           = "https://api.bybit.com"

DAYS        = int(os.getenv("FUNDING_DAYS", "1095"))      # 3 года
SYMBOLS     = os.getenv("FUNDING_SYMBOLS",
                        "BTCUSDT,ETHUSDT,SOLUSDT,XRPUSDT,DOGEUSDT,BNBUSDT,"
                        "ADAUSDT,LINKUSDT,AVAXUSDT,LTCUSDT").split(",")
SPOT_FEE    = 0.001       # спот taker
PERP_FEE    = 0.00055     # фьючерс taker
SLIP        = 0.0002      # на каждую ногу
ROUND_TRIP  = 2 * (SPOT_FEE + PERP_FEE + 2 * SLIP)   # вход+выход, обе ноги, доля от суммы
CAPITAL_MULT = 1.5        # капитал на 1$ позиции: 1$ спот + 0.5$ маржи
DAY_MS      = 86_400_000

GRID = {
    "LOOKBACK_D": [3, 7, 14],          # окно средней ставки funding
    "ENTER_APR":  [0.05, 0.10, 0.20],  # входим, если средняя годовая ставка выше
    "EXIT_APR":   [0.0, 0.03],         # выходим, если ниже
}
MIN_OK_APR   = 0.08       # вердикт: ≥ 8% годовых на капитал вне выборки
MAX_OK_DD    = -0.03      # и просадка не глубже −3% капитала


# ═══════════════ данные ═══════════════

def fetch_funding(symbol: str, days: int = DAYS) -> list[tuple[int, float]]:
    """История выплат funding: [(ts_ms, rate), ...] по возрастанию времени."""
    start = int(time.time() * 1000) - days * DAY_MS
    end = int(time.time() * 1000)
    rows: dict[int, float] = {}
    for _ in range(200):
        r = requests.get(f"{BYBIT}/v5/market/funding/history", params={
            "category": "linear", "symbol": symbol, "endTime": end, "limit": 200}, timeout=15)
        body = r.json()
        if body.get("retCode") != 0:
            raise RuntimeError(body.get("retMsg"))
        chunk = body["result"]["list"]
        if not chunk:
            break
        for c in chunk:
            rows[int(c["fundingRateTimestamp"])] = float(c["fundingRate"])
        oldest = min(int(c["fundingRateTimestamp"]) for c in chunk)
        if oldest <= start or len(chunk) < 200:
            break
        end = oldest - 1
        time.sleep(0.12)
    return sorted((t, v) for t, v in rows.items() if t >= start)


# ═══════════════ симуляция ═══════════════

def simulate(series, p=None, t_from=None, t_to=None) -> dict:
    """p=None — держим позицию весь период. Иначе правило входа/выхода
    по средней ставке за LOOKBACK_D дней (только прошлые данные)."""
    pts = [(t, r) for t, r in series if (t_from is None or t >= t_from) and (t_to is None or t < t_to)]
    if len(pts) < 10:
        return {"ok": False}
    hist = [x for x in series if t_from is not None and x[0] < t_from]  # разогрев окна
    in_pos = p is None
    pnl, equity, peak, mdd = 0.0, 0.0, 0.0, 0.0
    switches, time_in, fees = 0, 0, 0.0
    if in_pos:
        fees += ROUND_TRIP / 2
        pnl -= ROUND_TRIP / 2
    daily: dict[int, float] = {}
    window = [x for x in hist]
    for t, r in pts:
        # 1) выплата за этот период, если в позиции (шорт получает r, при r<0 платит)
        if in_pos:
            pnl += r
            time_in += 1
            daily[t // DAY_MS] = daily.get(t // DAY_MS, 0.0) + r
        # 2) решение после выплаты — на следующий период
        window.append((t, r))
        if p is not None:
            lb = t - p["LOOKBACK_D"] * DAY_MS
            recent = [x[1] for x in window if x[0] > lb]
            window = [x for x in window if x[0] > lb]
            if len(recent) >= 3:
                per_day = len(recent) / p["LOOKBACK_D"]
                apr = sum(recent) / len(recent) * per_day * 365
                if not in_pos and apr > p["ENTER_APR"]:
                    in_pos = True; switches += 1
                    pnl -= ROUND_TRIP / 2; fees += ROUND_TRIP / 2
                elif in_pos and apr < p["EXIT_APR"]:
                    in_pos = False; switches += 1
                    pnl -= ROUND_TRIP / 2; fees += ROUND_TRIP / 2
        equity = pnl
        peak = max(peak, equity); mdd = min(mdd, equity - peak)
    if in_pos:            # закрываем в конце периода
        pnl -= ROUND_TRIP / 2; fees += ROUND_TRIP / 2
    days = (pts[-1][0] - pts[0][0]) / DAY_MS or 1
    ret_cap = pnl / CAPITAL_MULT
    neg = sum(1 for v in daily.values() if v < 0)
    return {
        "ok": True, "days": round(days, 1),
        "return_on_capital": round(ret_cap * 100, 2),
        "apr_on_capital": round(ret_cap / days * 365 * 100, 2),
        "max_drawdown": round(mdd / CAPITAL_MULT * 100, 2),
        "time_in_position": round(time_in / len(pts) * 100, 1),
        "switches": switches, "fees_pct": round(fees / CAPITAL_MULT * 100, 2),
        "negative_days": neg,
    }


def portfolio(results: list[dict]) -> dict:
    ok = [r for r in results if r.get("ok")]
    if not ok:
        return {"ok": False}
    avg = lambda k: round(sum(r[k] for r in ok) / len(ok), 2)
    return {"ok": True, "coins": len(ok), "apr_on_capital": avg("apr_on_capital"),
            "max_drawdown": round(min(r["max_drawdown"] for r in ok), 2),
            "time_in_position": avg("time_in_position"),
            "switches": round(sum(r["switches"] for r in ok) / len(ok), 1)}


# ═══════════════ прогон ═══════════════

def run(progress=None) -> dict:
    data, skipped = {}, []
    for s in [x.strip() for x in SYMBOLS if x.strip()]:
        try:
            ser = fetch_funding(s)
            if len(ser) > 100:
                data[s] = ser
            else:
                skipped.append(s)
        except Exception:
            skipped.append(s)
    if not data:
        raise RuntimeError("не удалось загрузить историю funding")
    if progress:
        progress(f"📥 История funding загружена: {len(data)} монет. Считаю...")

    t0 = min(v[0][0] for v in data.values())
    t1 = max(v[-1][0] for v in data.values())
    split = t0 + int((t1 - t0) * 2 / 3)

    # 1) «держать всегда» — по каждой монете, весь период и последняя треть
    always = {s: {"full": simulate(v), "oos": simulate(v, None, split, None)} for s, v in data.items()}

    # 2) правило входа/выхода: подбор на 2/3, проверка на 1/3 (один набор для всех монет)
    grid = []
    for lb, en, ex in itertools.product(*GRID.values()):
        if ex >= en:
            continue
        p = {"LOOKBACK_D": lb, "ENTER_APR": en, "EXIT_APR": ex}
        ins = portfolio([simulate(v, p, t0, split) for v in data.values()])
        grid.append((p, ins))
    grid = [g for g in grid if g[1].get("ok")]
    best_p, best_in = max(grid, key=lambda g: (g[1]["apr_on_capital"], g[1]["max_drawdown"]))
    per_coin_oos = {s: simulate(v, best_p, split, None) for s, v in data.items()}
    rule_oos = portfolio(list(per_coin_oos.values()))
    always_oos = portfolio([a["oos"] for a in always.values()])

    best_oos = max([("rule", rule_oos), ("always", always_oos)], key=lambda x: x[1]["apr_on_capital"])
    verdict = ("OK" if best_oos[1]["apr_on_capital"] >= MIN_OK_APR * 100
               and best_oos[1]["max_drawdown"] >= MAX_OK_DD * 100 else "NO")

    report = {
        "version": FUNDING_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "days": round((t1 - t0) / DAY_MS), "oos_days": round((t1 - split) / DAY_MS),
        "symbols": sorted(data), "skipped": skipped,
        "costs": {"round_trip_pct": round(ROUND_TRIP * 100, 3), "capital_mult": CAPITAL_MULT},
        "always": always, "always_oos_portfolio": always_oos,
        "rule": {"params": best_p, "in_sample": best_in, "oos": rule_oos, "per_coin_oos": per_coin_oos},
        "best": best_oos[0], "verdict": verdict,
        "gate": {"min_apr_pct": MIN_OK_APR * 100, "max_dd_pct": MAX_OK_DD * 100},
        "not_modeled": "изменение базиса (спот vs фьючерс) при входе/выходе, риск ликвидации шорта "
                       "при резком росте, риск биржи",
    }
    os.makedirs("logs", exist_ok=True)
    with open(REPORT_FILE, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    with open(VERSION_FILE, "w") as f:
        f.write(FUNDING_VERSION)
    return report


def format_report(rep: dict) -> str:
    lines = [f"💰 <b>Арбитраж funding</b> (спот-лонг + фьючерс-шорт), {rep['days']} дн., "
             f"{len(rep['symbols'])} монет",
             f"Издержки вход+выход: {rep['costs']['round_trip_pct']}% суммы. "
             f"Доходность — на весь капитал (×{rep['costs']['capital_mult']}).", "",
             "<b>Держать всегда, весь период</b> (годовых на капитал):"]
    for s, a in sorted(rep["always"].items(), key=lambda x: -x[1]["full"].get("apr_on_capital", -99)):
        f = a["full"]
        if f.get("ok"):
            lines.append(f"  {s.replace('USDT','')}: {f['apr_on_capital']:+.1f}%, "
                         f"просадка {f['max_drawdown']}%")
    ao, ro, p = rep["always_oos_portfolio"], rep["rule"]["oos"], rep["rule"]["params"]
    lines += ["", f"<b>Проверка на последних {rep['oos_days']} дн.</b> (портфель, вне выборки):",
              f"  Держать всегда: {ao['apr_on_capital']:+.1f}% годовых, просадка {ao['max_drawdown']}%",
              f"  Правило (средняя за {p['LOOKBACK_D']} дн.: вход &gt;{p['ENTER_APR']*100:g}%, "
              f"выход &lt;{p['EXIT_APR']*100:g}%): {ro['apr_on_capital']:+.1f}% годовых, "
              f"просадка {ro['max_drawdown']}%, в позиции {ro['time_in_position']}% времени", ""]
    if rep["verdict"] == "OK":
        lines.append(f"✅ Проходит фильтр (≥{rep['gate']['min_apr_pct']:g}% годовых, "
                     f"просадка не глубже {rep['gate']['max_dd_pct']:g}%).")
    else:
        lines.append(f"⛔ Не проходит фильтр (нужно ≥{rep['gate']['min_apr_pct']:g}% годовых "
                     f"и просадка не глубже {rep['gate']['max_dd_pct']:g}%).")
    lines.append("⚠️ Не учтено: изменение базиса, риск ликвидации шорта, риск биржи.")
    return "\n".join(lines)


def needs_auto_run() -> bool:
    try:
        with open(VERSION_FILE) as f:
            return f.read().strip() != FUNDING_VERSION
    except FileNotFoundError:
        return True


if __name__ == "__main__":
    print(format_report(run(progress=print)))
