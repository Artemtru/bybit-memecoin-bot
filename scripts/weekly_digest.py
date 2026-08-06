#!/usr/bin/env python3
"""
╔══════════════════════════════════════════════════════════════╗
║  weekly_digest.py — Еженедельный дайджест по сделкам бота    ║
║                                                              ║
║  Собирает статистику за 7 дней через analytics.py и шлёт    ║
║  сводку в Telegram-чат бота через telegram_notifier.py.     ║
║                                                              ║
Запуск (cron), понедельник 10:00:
 0 10 * * 1 cd /root/bybit-memecoin-bot && venv/bin/python scripts/weekly_digest.py >> reports/digest.log 2>&1
"""

import os
import sys
from datetime import datetime
from pathlib import Path

BOT_DIR = Path(os.getenv("BOT_DIR", "/root/bybit-memecoin-bot"))
os.chdir(BOT_DIR)
sys.path.insert(0, str(BOT_DIR))

# .env нужен для TELEGRAM_TOKEN / TELEGRAM_CHAT_ID
try:
    from dotenv import load_dotenv
    load_dotenv(BOT_DIR / ".env")
except ImportError:
    pass

try:
    import analytics
    import telegram_notifier as tg
except ImportError as e:
    print(f"❌ Импорт не удался из {BOT_DIR}: {e}")
    sys.exit(1)


def build_digest() -> str:
    """Собрать текст дайджеста за 7 дней."""
    s = analytics.last_n_days(7)
    now = datetime.now()

    if s["count"] == 0:
        return (
            f"📭 <b>Еженедельный дайджест бота</b>\n"
            f"<i>{now:%d.%m.%Y}</i>\n\n"
            f"За последние 7 дней сделок не было.\n"
            f"Бот сканирует рынок и ждёт качественные сетапы."
        )

    pnl_sign = "+" if s["pnl"] >= 0 else ""
    pf = s["profit_factor"]
    pf_str = f"{pf:.2f}" if pf < 100 else "∞"
    verdict = "🟢 прибыльная" if s["pnl"] > 0 else ("🔴 убыточная" if s["pnl"] < 0 else "⚪ в ноль")

    lines = [
        f"📊 <b>Еженедельный дайджест бота</b>",
        f"<i>{now:%d.%m.%Y} · за 7 дней</i>",
        "",
        f"Итог недели: {verdict}",
        f"PnL: <b>{pnl_sign}{s['pnl']:.2f} USDT</b>",
        "",
        f"Сделок: {s['count']}  |  Winrate: {s['winrate']}%",
        f"✅ Побед: {s['wins']}  |  ❌ Потерь: {s['losses']}",
        f"Profit Factor: {pf_str}",
        f"Ср. выигрыш: +{s['avg_win']:.3f}  |  ср. потеря: {s['avg_loss']:.3f}",
        f"Макс. просадка: -{s['max_drawdown']:.2f} USDT",
        "",
        f"↔️ Боковик: {s['sideways_pnl']:+.2f}$ (WR {s['sideways_wr']}%)",
        f"📈 Тренд:   {s['trend_pnl']:+.2f}$ (WR {s['trend_wr']}%)",
    ]

    # Топ / анти-топ монеты
    by_sym = s.get("by_symbol", {})
    if by_sym:
        ranked = sorted(by_sym.items(), key=lambda x: x[1]["pnl"], reverse=True)
        lines.append("")
        lines.append("<b>По монетам:</b>")
        # Топ-3 прибыльных
        for sym, d in ranked[:3]:
            if d["pnl"] <= 0:
                break
            wr = round(d["wins"] / d["count"] * 100) if d["count"] else 0
            lines.append(f"  🟢 {sym}: +{d['pnl']:.2f}$ ({d['count']} сд., WR {wr}%)")
        # Анти-топ (убыточные) — кандидаты на исключение
        losers = [(sym, d) for sym, d in ranked if d["pnl"] < 0]
        if losers:
            lines.append("")
            lines.append("<b>⚠️ Убыточные (кандидаты на исключение):</b>")
            for sym, d in losers[-3:]:
                wr = round(d["wins"] / d["count"] * 100) if d["count"] else 0
                lines.append(f"  🔴 {sym}: {d['pnl']:.2f}$ ({d['count']} сд., WR {wr}%)")

    return "\n".join(lines)


def main():
    print(f"\n📊 [{datetime.now():%Y-%m-%d %H:%M:%S}] Формирую еженедельный дайджест")

    if not (os.getenv("TELEGRAM_TOKEN") and os.getenv("TELEGRAM_CHAT_ID")):
        print("⚠️  TELEGRAM_TOKEN / TELEGRAM_CHAT_ID не заданы в .env — отправка невозможна")
        # Всё равно печатаем в лог для диагностики
        print(build_digest())
        return

    text = build_digest()
    tg.send_message(text)
    print("✅ Дайджест отправлен в Telegram")
    print("---")
    print(text)


if __name__ == "__main__":
    main()
