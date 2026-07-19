"""
╔══════════════════════════════════════════════╗
║         ANALYTICS.PY — Аналитика PnL        ║
║  Считает статистику по периодам.             ║
║  Используется strategy_adapter и Telegram.  ║
╚══════════════════════════════════════════════╝
"""

from datetime import datetime
from pnl_tracker import get_all_trades, get_trades_by_period, get_trades_since


def _stats(trades: list) -> dict:
    """Посчитать статистику по списку сделок."""
    if not trades:
        return {
            "count": 0, "wins": 0, "losses": 0, "winrate": 0,
            "pnl": 0, "avg_win": 0, "avg_loss": 0, "profit_factor": 0,
            "max_drawdown": 0, "best_trade": 0, "worst_trade": 0,
            "sideways_pnl": 0, "trend_pnl": 0,
            "by_symbol": {},
        }

    wins   = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] <= 0]

    total_win  = sum(t["pnl"] for t in wins)
    total_loss = abs(sum(t["pnl"] for t in losses))

    # Максимальная просадка (drawdown)
    equity = 0
    peak = 0
    max_dd = 0
    for t in trades:
        equity += t["pnl"]
        peak = max(peak, equity)
        dd = peak - equity
        max_dd = max(max_dd, dd)

    # По символу
    by_symbol = {}
    for t in trades:
        s = t["symbol"]
        if s not in by_symbol:
            by_symbol[s] = {"count": 0, "pnl": 0, "wins": 0}
        by_symbol[s]["count"] += 1
        by_symbol[s]["pnl"]   += t["pnl"]
        if t["pnl"] > 0:
            by_symbol[s]["wins"] += 1

    # По режиму рынка
    sw = [t for t in trades if t.get("mode") == "sideways"]
    tr = [t for t in trades if t.get("mode") == "trend"]

    return {
        "count":         len(trades),
        "wins":          len(wins),
        "losses":        len(losses),
        "winrate":       round(len(wins) / len(trades) * 100, 1) if trades else 0,
        "pnl":           round(sum(t["pnl"] for t in trades), 4),
        "avg_win":       round(total_win / len(wins), 4) if wins else 0,
        "avg_loss":      round(-total_loss / len(losses), 4) if losses else 0,
        "profit_factor": round(total_win / total_loss, 2) if total_loss > 0 else 999,
        "max_drawdown":  round(max_dd, 4),
        "best_trade":    round(max(t["pnl"] for t in trades), 4),
        "worst_trade":   round(min(t["pnl"] for t in trades), 4),
        "sideways_pnl":  round(sum(t["pnl"] for t in sw), 4),
        "trend_pnl":     round(sum(t["pnl"] for t in tr), 4),
        "sideways_wr":   round(len([t for t in sw if t["pnl"]>0]) / len(sw) * 100, 1) if sw else 0,
        "trend_wr":      round(len([t for t in tr if t["pnl"]>0]) / len(tr) * 100, 1) if tr else 0,
        "by_symbol":     by_symbol,
    }


def today():
    key = datetime.now().strftime("%Y-%m-%d")
    return _stats(get_trades_by_period("date", key))


def this_week():
    key = datetime.now().strftime("%Y-W%W")
    return _stats(get_trades_by_period("week", key))


def this_month():
    key = datetime.now().strftime("%Y-%m")
    return _stats(get_trades_by_period("month", key))


def this_quarter():
    now = datetime.now()
    key = f"{now.year}-Q{(now.month-1)//3+1}"
    return _stats(get_trades_by_period("quarter", key))


def this_year():
    key = str(datetime.now().year)
    return _stats(get_trades_by_period("year", key))


def last_n_days(n: int):
    return _stats(get_trades_since(n))


def all_time():
    return _stats(get_all_trades())


def format_report(stats: dict, label: str) -> str:
    """Форматировать статистику для Telegram."""
    if stats["count"] == 0:
        return f"📊 <b>{label}</b>\nСделок пока нет."

    pnl_sign = "+" if stats["pnl"] >= 0 else ""
    pf = stats["profit_factor"]
    pf_str = f"{pf:.2f}" if pf < 100 else "∞"

    lines = [
        f"📊 <b>{label}</b>",
        f"",
        f"Сделок: {stats['count']}  |  Winrate: {stats['winrate']}%",
        f"Победы: {stats['wins']}  |  Потери: {stats['losses']}",
        f"PnL: <b>{pnl_sign}{stats['pnl']} USDT</b>",
        f"Profit Factor: {pf_str}",
        f"",
        f"Ср. выигрыш: +{stats['avg_win']} USDT",
        f"Ср. потеря: {stats['avg_loss']} USDT",
        f"Лучшая сделка: +{stats['best_trade']} USDT",
        f"Худшая сделка: {stats['worst_trade']} USDT",
        f"Макс. просадка: -{stats['max_drawdown']} USDT",
        f"",
        f"↔️ Боковик: {stats['sideways_pnl']:+.4f} USDT (WR {stats['sideways_wr']}%)",
        f"📈 Тренд:   {stats['trend_pnl']:+.4f} USDT (WR {stats['trend_wr']}%)",
    ]

    if stats["by_symbol"]:
        lines.append("")
        lines.append("По монетам:")
        for sym, d in sorted(stats["by_symbol"].items(),
                             key=lambda x: x[1]["pnl"], reverse=True):
            wr = round(d["wins"]/d["count"]*100, 0) if d["count"] else 0
            sign = "+" if d["pnl"] >= 0 else ""
            lines.append(f"  {sym}: {sign}{d['pnl']:.4f}$ ({d['count']} сд., WR {wr:.0f}%)")

    return "\n".join(lines)
