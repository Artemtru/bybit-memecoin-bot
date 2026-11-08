"""
╔══════════════════════════════════════════════════════╗
║   BACKTEST.PY — Моделирование стратегии бота         ║
║                                                      ║
║  Прогоняет ТУ ЖЕ логику, что manager.py (BB+RSI,     ║
║  ATR TP/SL, ADX-фильтр, режимы sideways/trend,       ║
║  cooldown, серия стопов, дневной лимит) на истории   ║
║  свечей Bybit. Ордеров НЕ ставит, ключи НЕ нужны —   ║
║  только публичные котировки.                         ║
║                                                      ║
║  Методика по образцу HKUDS/Vibe-Trading (crypto      ║
║  engine + metrics): taker-комиссия с обеих сторон,   ║
║  проскальзывание, profit factor / max drawdown,      ║
║  проверка вне выборки (walk-forward).                ║
╚══════════════════════════════════════════════════════╝

Запуск вручную:  python backtest.py
Из Telegram:     /backtest
"""

from __future__ import annotations

import itertools
import json
import os
import subprocess
import tempfile
import time
from datetime import datetime, timezone

import requests
from dotenv import load_dotenv

load_dotenv()

BACKTEST_VERSION = "2026-11-08.2"   # смена версии → авто-прогон после деплоя
REPORT_FILE      = "logs/backtest_report.json"
VERSION_FILE     = "logs/backtest_version.txt"
BYBIT            = "https://api.bybit.com"

DAYS       = int(os.getenv("BACKTEST_DAYS", "90"))   # 90 дн. → 50+ сделок для выводов
INTERVAL   = os.getenv("INTERVAL", "15")
TAKER_FEE  = 0.00055      # Bybit taker, каждая сторона
SLIPPAGE   = 0.0005       # 0.05% на вход и выход по рынку
WINDOW     = 150          # бот берёт 150 свечей (get_klines limit=150)
DEFAULT_SYMBOLS = (
    "DOGEUSDT,1000PEPEUSDT,WIFUSDT,1000BONKUSDT,POPCATUSDT,1000FLOKIUSDT,"
    "SHIB1000USDT,BRETTUSDT,MEWUSDT,NEIROETHUSDT,GOATUSDT,PNUTUSDT,FARTCOINUSDT,TRUMPUSDT"
)


# ═══════════════ данные ═══════════════

def fetch_klines(symbol: str, days: int = DAYS, interval: str = INTERVAL):
    """Публичные свечи Bybit (без ключа), постранично назад во времени."""
    minutes = int(interval)
    need = days * 24 * 60 // minutes
    end = int(time.time() * 1000)
    rows: dict[int, list] = {}
    while len(rows) < need:
        r = requests.get(f"{BYBIT}/v5/market/kline", params={
            "category": "linear", "symbol": symbol, "interval": interval,
            "limit": 1000, "end": end}, timeout=15)
        body = r.json()
        if body.get("retCode") != 0:
            raise RuntimeError(body.get("retMsg"))
        chunk = body["result"]["list"]
        if not chunk:
            break
        for c in chunk:
            rows[int(c[0])] = c
        end = int(chunk[-1][0]) - 1
        if len(chunk) < 1000:
            break
        time.sleep(0.15)
    bars = sorted(rows.values(), key=lambda c: int(c[0]))
    # последнюю (незакрытую) свечу отбрасываем
    bars = bars[:-1][-need:]
    return {
        "ts": [int(b[0]) for b in bars],
        "open": [float(b[1]) for b in bars], "high": [float(b[2]) for b in bars],
        "low": [float(b[3]) for b in bars], "close": [float(b[4]) for b in bars],
        "volume": [float(b[5]) for b in bars],
    }


# ═══════════════ индикаторы (копия manager.py 1:1) ═══════════════

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
    return 100.0 if al == 0 else 100 - 100 / (1 + ag / al)


def calc_atr(closes, highs, lows, period=14):
    trs = [max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1]))
           for i in range(1, len(closes))]
    if len(trs) < period:
        return None
    atr = sum(trs[:period]) / period
    for tr in trs[period:]:
        atr = (atr * (period - 1) + tr) / period
    return atr


def calc_ema(closes, period):
    if len(closes) < period:
        return None
    ema = sum(closes[:period]) / period
    k = 2 / (period + 1)
    for p in closes[period:]:
        ema = p * k + ema * (1 - k)
    return ema


def calc_adx(closes, highs, lows, period=14):
    n = len(closes)
    if n < period * 2 + 1:
        return None
    pdm, mdm, trs = [], [], []
    for i in range(1, n):
        up, dn = highs[i] - highs[i - 1], lows[i - 1] - lows[i]
        pdm.append(up if (up > dn and up > 0) else 0.0)
        mdm.append(dn if (dn > up and dn > 0) else 0.0)
        trs.append(max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1])))

    def sm(v):
        s = [sum(v[:period])]
        for x in v[period:]:
            s.append(s[-1] - s[-1] / period + x)
        return s

    dxs = []
    for a, p, m in zip(sm(trs), sm(pdm), sm(mdm)):
        if a == 0:
            continue
        pdi, mdi = 100 * p / a, 100 * m / a
        if pdi + mdi == 0:
            continue
        dxs.append(100 * abs(pdi - mdi) / (pdi + mdi))
    if len(dxs) < period:
        return None
    adx = sum(dxs[:period]) / period
    for dx in dxs[period:]:
        adx = (adx * (period - 1) + dx) / period
    return adx


def precompute(d, bb_period=20):
    """Индикаторы на каждом баре по окну 150 свечей — как бот на каждом тике."""
    n = len(d["close"])
    feats = [None] * n
    for i in range(WINDOW - 1, n):
        c = d["close"][i - WINDOW + 1:i + 1]
        h = d["high"][i - WINDOW + 1:i + 1]
        l = d["low"][i - WINDOW + 1:i + 1]
        v = d["volume"][i - WINDOW + 1:i + 1]
        w = c[-bb_period:]
        mid = sum(w) / bb_period
        std = (sum((x - mid) ** 2 for x in w) / bb_period) ** 0.5
        feats[i] = {
            "rsi": calc_rsi(c), "atr": calc_atr(c, h, l), "adx": calc_adx(c, h, l),
            "ema20": calc_ema(c, 20), "ema50": calc_ema(c, 50),
            "mid": mid, "std": std, "prev": c[-2],
            "vol_ratio": v[-1] / (sum(v[-20:-1]) / 19) if sum(v[-20:-1]) > 0 else 0,
        }
    return feats


# ═══════════════ параметры ═══════════════

def env_params() -> dict:
    g = os.getenv
    return {
        "RSI_BUY": float(g("RSI_BUY", "42")), "RSI_SELL": float(g("RSI_SELL", "58")),
        "BB_STD": float(g("BB_STD", "1.5")),
        "ATR_TP_MULT": float(g("ATR_TP_MULT", "2.5")), "ATR_SL_MULT": float(g("ATR_SL_MULT", "1.2")),
        "SIDEWAYS_ATR_THRESHOLD": float(g("SIDEWAYS_ATR_THRESHOLD", "0.015")),
        "ADX_TREND_THRESHOLD": float(g("ADX_TREND_THRESHOLD", "25")),
        "VOL_FILTER": 1.3,
        "STOP_COOLDOWN_MIN": float(g("STOP_COOLDOWN_MIN", "20")),
        "DAILY_LOSS_LIMIT": float(g("DAILY_LOSS_LIMIT", "-2")),
        "QTY_USDT": float(g("QTY_USDT", "20")),
        "MIN_POSITION_USDT": float(g("MIN_POSITION_USDT", "10")),
        "MAX_POSITION_USDT": float(g("MAX_POSITION_USDT", "30")),
        "MAX_BOTS": int(float(g("MAX_BOTS", "3"))),
    }


# ═══════════════ симуляция ═══════════════

def mode_of(f, price, p):
    if f["atr"] is None:
        return "trend"
    atr_pct = f["atr"] / price
    if f["ema20"] and f["ema50"]:
        side = atr_pct < p["SIDEWAYS_ATR_THRESHOLD"] and abs(f["ema20"] - f["ema50"]) / price < 0.02
    else:
        side = atr_pct < p["SIDEWAYS_ATR_THRESHOLD"]
    if f["adx"] is not None and f["adx"] >= p["ADX_TREND_THRESHOLD"]:
        side = False
    return "sideways" if side else "trend"


def sideways_signal(f, price, p):
    upper = f["mid"] + p["BB_STD"] * f["std"]
    lower = f["mid"] - p["BB_STD"] * f["std"]
    vol_ok = p["VOL_FILTER"] <= 0 or f["vol_ratio"] > p["VOL_FILTER"]
    if f["prev"] <= lower and price > lower and f["rsi"] < p["RSI_BUY"] and vol_ok:
        return "Buy"
    if f["prev"] >= upper and price < upper and f["rsi"] > p["RSI_SELL"] and vol_ok:
        return "Sell"
    return None


def simulate(data: dict, feats: dict, p: dict, t_from=None, t_to=None) -> list:
    """Все монеты синхронно по времени, общие лимиты — как в manager.py."""
    timeline = sorted({t for d in data.values() for t in d["ts"]
                       if (t_from is None or t >= t_from) and (t_to is None or t < t_to)})
    idx = {s: {t: i for i, t in enumerate(d["ts"])} for s, d in data.items()}
    pos: dict[str, dict] = {}
    pending: dict[str, tuple] = {}         # вход/выход по открытию следующей свечи
    last_stop: dict[str, int] = {}
    trades, day, day_pnl, streak, block_until = [], None, 0.0, 0, 0

    def close(sym, px, t, why):
        nonlocal day_pnl, streak, block_until
        ps = pos.pop(sym)
        sgn = 1 if ps["side"] == "Buy" else -1
        exit_px = px * (1 - sgn * SLIPPAGE) if why != "TP" else px
        gross = sgn * (exit_px - ps["entry"]) * ps["qty"]
        fees = (ps["entry"] + exit_px) * ps["qty"] * TAKER_FEE
        pnl = gross - fees
        trades.append({"symbol": sym, "side": ps["side"], "entry_ts": ps["ts"], "exit_ts": t,
                       "pnl": pnl, "result": why, "notional": ps["entry"] * ps["qty"]})
        day_pnl += pnl
        if pnl < 0:
            last_stop[sym] = t
            streak += 1
            if streak >= 3:
                block_until = t + 90 * 60_000    # адаптер снимает блок ~раз в 3 скана
                streak = 0
        else:
            streak = 0

    for t in timeline:
        d_key = datetime.fromtimestamp(t / 1000, timezone.utc).date()
        if d_key != day:
            day, day_pnl, streak, block_until = d_key, 0.0, 0, 0
        for sym, d in data.items():
            i = idx[sym].get(t)
            if i is None:
                continue
            o, h, l, c = d["open"][i], d["high"][i], d["low"][i], d["close"][i]
            # 1) исполнить отложенное по open
            if sym in pending:
                kind, side, atr = pending.pop(sym)
                if kind == "exit" and sym in pos:
                    close(sym, o, t, "signal")
                elif kind == "entry" and sym not in pos:
                    sgn = 1 if side == "Buy" else -1
                    entry = o * (1 + sgn * SLIPPAGE)
                    min_move = entry * 0.005
                    tp_m = max(atr * p["ATR_TP_MULT"], min_move * 2)
                    sl_m = max(atr * p["ATR_SL_MULT"], min_move)
                    atr_pct_percent = atr / entry * 100        # как в боте (в процентах)
                    notional = max(min(p["QTY_USDT"] / (1 + atr_pct_percent * 10),
                                       p["MAX_POSITION_USDT"]), p["MIN_POSITION_USDT"])
                    pos[sym] = {"side": side, "entry": entry, "qty": notional / entry, "ts": t,
                                "tp": entry + sgn * tp_m, "sl": entry - sgn * sl_m}
            # 2) TP/SL внутри свечи (оба задеты → считаем SL, консервативно)
            if sym in pos:
                ps = pos[sym]
                buy = ps["side"] == "Buy"
                hit_sl = l <= ps["sl"] if buy else h >= ps["sl"]
                hit_tp = h >= ps["tp"] if buy else l <= ps["tp"]
                if hit_sl:
                    close(sym, ps["sl"], t, "SL")
                elif hit_tp:
                    close(sym, ps["tp"], t, "TP")
            # 3) решение на закрытии свечи
            f = feats[sym][i]
            if f is None or day_pnl <= p["DAILY_LOSS_LIMIT"]:
                continue
            mode = mode_of(f, c, p)
            if sym in pos:
                ps = pos[sym]
                sgn = 1 if ps["side"] == "Buy" else -1
                in_profit = sgn * (c - ps["entry"]) > 0
                if mode == "sideways":
                    sig = sideways_signal(f, c, p)
                else:
                    sig = "Buy" if f["rsi"] < p["RSI_BUY"] else ("Sell" if f["rsi"] > p["RSI_SELL"] else None)
                if sig and sig != ps["side"] and in_profit:
                    pending[sym] = ("exit", None, None)
                continue
            if mode != "sideways" or t < block_until or f["atr"] is None:
                continue
            if sym in last_stop and t - last_stop[sym] < p["STOP_COOLDOWN_MIN"] * 60_000:
                continue
            if len(pos) >= p["MAX_BOTS"]:
                continue
            sig = sideways_signal(f, c, p)
            if sig:
                pending[sym] = ("entry", sig, f["atr"])
    # незакрытые — по последней цене
    for sym in list(pos):
        d = data[sym]
        close(sym, d["close"][-1], d["ts"][-1], "end")
    return trades


def metrics(trades: list, days: float) -> dict:
    """Определения как в Vibe-Trading backtest/metrics.py."""
    n = len(trades)
    pnl = [x["pnl"] for x in trades]
    wins = [x for x in pnl if x > 0]
    loss = [x for x in pnl if x <= 0]
    gp, gl = sum(wins), -sum(loss)
    eq, peak, mdd = 0.0, 0.0, 0.0
    for x in sorted(trades, key=lambda z: z["exit_ts"]):
        eq += x["pnl"]; peak = max(peak, eq); mdd = min(mdd, eq - peak)
    return {
        "trades": n, "trades_per_day": round(n / days, 2) if days else 0,
        "win_rate": round(len(wins) / n * 100, 1) if n else 0,
        "total_pnl": round(sum(pnl), 2),
        "profit_factor": round(gp / gl, 2) if gl > 1e-9 else (99.0 if gp > 0 else 0.0),
        "avg_win": round(gp / len(wins), 3) if wins else 0,
        "avg_loss": round(-gl / len(loss), 3) if loss else 0,
        "max_drawdown": round(mdd, 2),
        "fees_note": f"taker {TAKER_FEE*100:.3f}% x2 + slippage {SLIPPAGE*100:.2f}% x2",
    }


# ═══════════════ прогон ═══════════════

MIN_TRADES_IN  = 30     # минимум сделок в выборке подбора
MIN_TRADES_OOS = 20     # минимум сделок вне выборки
MIN_PF_OOS     = 1.3    # минимальный profit factor вне выборки

GRID = {
    "RSI": [(30, 70), (35, 65), (42, 58)],
    "VOL_FILTER": [1.3, 0],
    "TPSL": [(3.0, 1.0), (2.0, 1.5), (1.5, 1.0)],
    "BB_STD": [2.0, 2.5],
    "SIDEWAYS_ATR_THRESHOLD": [0.015, 0.03],
}


def run(symbols=None, days=DAYS, progress=None) -> dict:
    symbols = symbols or os.getenv("BACKTEST_SYMBOLS", DEFAULT_SYMBOLS).split(",")
    data, skipped = {}, []
    for s in symbols:
        s = s.strip()
        try:
            d = fetch_klines(s, days)
            if len(d["close"]) > WINDOW + 50:
                data[s] = d
            else:
                skipped.append(s)
        except Exception:
            skipped.append(s)
    if not data:
        raise RuntimeError("не удалось загрузить свечи ни по одной монете")
    if progress:
        progress(f"📥 Свечи загружены: {len(data)} монет × {days} дн. Считаю...")
    feats = {s: precompute(d) for s, d in data.items()}

    t0 = min(d["ts"][WINDOW] for d in data.values())
    t1 = max(d["ts"][-1] for d in data.values())
    split = t0 + int((t1 - t0) * 2 / 3)         # 2/3 — подбор, 1/3 — проверка вне выборки
    d_in = (split - t0) / 86_400_000
    d_out = (t1 - split) / 86_400_000

    base = env_params()
    live_like = dict(base, RSI_BUY=42.0, RSI_SELL=58.0)   # adapt_rsi_thresholds() в манагере
    results = {
        "current_env": {"params": base,
                        "in_sample": metrics(simulate(data, feats, base, t0, split), d_in),
                        "out_of_sample": metrics(simulate(data, feats, base, split, None), d_out)},
        "live_actual_rsi_42_58": {"params": live_like,
                        "in_sample": metrics(simulate(data, feats, live_like, t0, split), d_in),
                        "out_of_sample": metrics(simulate(data, feats, live_like, split, None), d_out)},
    }

    grid = []
    for (rb, rs), vf, (tp, sl), bb, sat in itertools.product(*GRID.values()):
        p = dict(base, RSI_BUY=rb, RSI_SELL=rs, VOL_FILTER=vf, ATR_TP_MULT=tp,
                 ATR_SL_MULT=sl, BB_STD=bb, SIDEWAYS_ATR_THRESHOLD=sat)
        m = metrics(simulate(data, feats, p, t0, split), d_in)
        grid.append((p, m))
    # ранжируем по прибыли в выборке, только с достаточным числом сделок
    ranked = sorted([g for g in grid if g[1]["trades"] >= MIN_TRADES_IN],
                    key=lambda g: (g[1]["profit_factor"], g[1]["total_pnl"]), reverse=True)[:5]
    top = []
    for p, m in ranked:
        oos = metrics(simulate(data, feats, p, split, None), d_out)
        top.append({"params": {k: p[k] for k in ("RSI_BUY", "RSI_SELL", "VOL_FILTER", "ATR_TP_MULT",
                                                  "ATR_SL_MULT", "BB_STD", "SIDEWAYS_ATR_THRESHOLD")},
                    "in_sample": m, "out_of_sample": oos})

    # Строгий фильтр: вне выборки достаточно сделок, PF ≥ 1.3, прибыль,
    # и обязательно лучше текущих настроек и по PF, и по PnL.
    cur_oos = results["current_env"]["out_of_sample"]
    best = next((x for x in top
                 if x["out_of_sample"]["trades"] >= MIN_TRADES_OOS
                 and x["out_of_sample"]["profit_factor"] >= MIN_PF_OOS
                 and x["out_of_sample"]["total_pnl"] > 0
                 and x["out_of_sample"]["profit_factor"] > cur_oos["profit_factor"]
                 and x["out_of_sample"]["total_pnl"] > cur_oos["total_pnl"]), None)
    if best:
        verdict = "SWITCH"
    elif cur_oos["trades"] >= MIN_TRADES_OOS and cur_oos["profit_factor"] >= 1.0 and cur_oos["total_pnl"] > 0:
        verdict = "KEEP"
    elif cur_oos["trades"] < MIN_TRADES_OOS:
        verdict = "TOO_FEW_TRADES"
    else:
        verdict = "STOP"
    report = {
        "version": BACKTEST_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "interval_min": int(INTERVAL), "days": days,
        "in_sample_days": round(d_in, 1), "out_of_sample_days": round(d_out, 1),
        "symbols": sorted(data), "skipped": skipped,
        "variants_tested": len(grid),
        "results": results, "top_in_sample": top,
        "recommendation": best,
        "verdict": verdict,
        "gate": {"min_trades_oos": MIN_TRADES_OOS, "min_pf_oos": MIN_PF_OOS,
                 "must_beat_current": True},
        "note": "сигналы по закрытым свечам, один вход на свечу — как в manager.py после 2026-11-08",
    }
    os.makedirs("logs", exist_ok=True)
    with open(REPORT_FILE, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    with open(VERSION_FILE, "w") as f:
        f.write(BACKTEST_VERSION)
    return report


def _line(name, r):
    o = r["out_of_sample"]
    return (f"<b>{name}</b>\n  вне выборки: {o['trades']} сд., WR {o['win_rate']}%, "
            f"PF {o['profit_factor']}, PnL {o['total_pnl']:+.2f}$, DD {o['max_drawdown']}$")


def format_report(rep: dict) -> str:
    lines = [f"🧪 <b>Моделирование стратегии</b> ({rep['days']} дн., {rep['interval_min']}m, "
             f"{len(rep['symbols'])} монет)",
             f"Подбор: {rep['in_sample_days']} дн. → проверка: {rep['out_of_sample_days']} дн.\n",
             _line("Текущие настройки .env", rep["results"]["current_env"]),
             _line("Вариант RSI 42/58", rep["results"]["live_actual_rsi_42_58"]), ""]
    v = rep.get("verdict")
    if v == "SWITCH":
        b = rep["recommendation"]; pp = b["params"]
        lines += ["✅ <b>Найдена конфигурация лучше текущей</b>",
                  f"RSI {pp['RSI_BUY']:g}/{pp['RSI_SELL']:g}, BB {pp['BB_STD']}σ, "
                  f"TP×{pp['ATR_TP_MULT']} SL×{pp['ATR_SL_MULT']}, "
                  f"объём-фильтр {'вкл' if pp['VOL_FILTER'] else 'выкл'}, "
                  f"боковик ATR&lt;{pp['SIDEWAYS_ATR_THRESHOLD']*100:g}%",
                  _line("Результат", b)]
    elif v == "KEEP":
        lines += ["✅ <b>Текущие настройки проходят проверку</b> — менять нечего.",
                  "Ни один вариант не оказался надёжно лучше."]
    elif v == "TOO_FEW_TRADES":
        lines += ["⚠️ <b>Слишком мало сделок для вывода</b>",
                  f"Нужно ≥{MIN_TRADES_OOS} сделок вне выборки. Настройки не меняю."]
    else:
        lines += ["⛔ <b>Текущая стратегия убыточна вне выборки</b>",
                  f"И ни один из {rep['variants_tested']} вариантов не прошёл фильтр. "
                  "Живую торговлю лучше приостановить."]
    return "\n".join(lines)


def publish_report_to_github() -> str:
    """Кладёт отчёт в ветку backtest-results (не main → деплой не запускается)."""
    try:
        remote = subprocess.check_output(["git", "remote", "get-url", "origin"], text=True).strip()
        with tempfile.TemporaryDirectory() as tmp:
            g = ["git", "-C", tmp, "-c", "user.name=bybit-bot", "-c", "user.email=bot@localhost"]
            subprocess.run(["git", "init", "-q", tmp], check=True)
            subprocess.run(g + ["remote", "add", "origin", remote], check=True)
            if subprocess.run(g + ["fetch", "-q", "--depth", "1", "origin", "backtest-results"]).returncode == 0:
                subprocess.run(g + ["checkout", "-q", "-b", "backtest-results", "FETCH_HEAD"], check=True)
            else:
                subprocess.run(g + ["checkout", "-q", "--orphan", "backtest-results"], check=True)
            with open(REPORT_FILE, encoding="utf-8") as src, \
                    open(os.path.join(tmp, "backtest_report.json"), "w", encoding="utf-8") as dst:
                dst.write(src.read())
            subprocess.run(g + ["add", "backtest_report.json"], check=True)
            subprocess.run(g + ["commit", "-q", "-m", f"backtest {BACKTEST_VERSION}"], check=True)
            subprocess.run(g + ["push", "-q", "origin", "HEAD:backtest-results"], check=True, timeout=60)
        return "отчёт опубликован в ветку backtest-results"
    except Exception as e:
        return f"не удалось опубликовать отчёт в GitHub: {e.__class__.__name__}"


def needs_auto_run() -> bool:
    try:
        with open(VERSION_FILE) as f:
            return f.read().strip() != BACKTEST_VERSION
    except FileNotFoundError:
        return True


if __name__ == "__main__":
    rep = run(progress=print)
    print(format_report(rep))
    print(publish_report_to_github())
