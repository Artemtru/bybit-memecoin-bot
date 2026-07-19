"""
╔══════════════════════════════════════════════╗
║         PNL_TRACKER.PY — База сделок        ║
║  Записывает каждую сделку в JSON-файл.      ║
║  Импортируется в manager.py                 ║
╚══════════════════════════════════════════════╝
"""

import os
import json
from datetime import datetime

TRADES_FILE = "logs/trades.json"


def _load():
    if not os.path.exists(TRADES_FILE):
        return []
    try:
        with open(TRADES_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []


def _save(trades):
    os.makedirs("logs", exist_ok=True)
    with open(TRADES_FILE, "w", encoding="utf-8") as f:
        json.dump(trades, f, ensure_ascii=False, indent=2)


def record_trade(
    symbol: str,
    side: str,       # "Buy" / "Sell"
    result: str,     # "TP" / "SL" / "signal" (закрыт по RSI/BB-сигналу)
    pnl: float,
    entry_price: float = None,
    exit_price: float  = None,
    mode: str = None,  # "sideways" / "trend"
):
    """Записать сделку в базу."""
    trades = _load()
    trade = {
        "id":          len(trades) + 1,
        "ts":          datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "date":        datetime.now().strftime("%Y-%m-%d"),
        "week":        datetime.now().strftime("%Y-W%W"),
        "month":       datetime.now().strftime("%Y-%m"),
        "quarter":     f"{datetime.now().year}-Q{(datetime.now().month-1)//3+1}",
        "year":        str(datetime.now().year),
        "symbol":      symbol,
        "side":        side,
        "result":      result,
        "pnl":         round(pnl, 6),
        "entry_price": entry_price,
        "exit_price":  exit_price,
        "mode":        mode,
    }
    trades.append(trade)
    _save(trades)
    return trade


def get_all_trades():
    return _load()


def get_trades_by_period(period_key: str, period_val: str):
    """
    period_key: "date" | "week" | "month" | "quarter" | "year"
    period_val: "2026-06-21" | "2026-W25" | "2026-06" | "2026-Q2" | "2026"
    """
    return [t for t in _load() if t.get(period_key) == period_val]


def get_trades_since(days: int):
    """Последние N дней."""
    from datetime import timedelta
    cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    return [t for t in _load() if t.get("date", "") >= cutoff]
