"""
╔══════════════════════════════════════════════╗
║      TELEGRAM_COMMANDS.PY — Бот-команды     ║
║   Отдельный процесс. Отвечает на команды    ║
║   в Telegram, читая статус из logs/status.json
╚══════════════════════════════════════════════╝

Запуск:  python telegram_commands.py
Стоп:    Ctrl+C

Команды в Telegram:
  /status  — общий статус (боты, PnL)
  /coins   — детали по каждой монете
  /pnl     — текущий PnL за день
  /top     — свежий скан топ-монет рынка прямо сейчас
  /help    — список команд
"""

import os
import json
import time
import requests
from dotenv import load_dotenv

from scanner import scan
import analytics

load_dotenv()

TELEGRAM_TOKEN   = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
STATUS_FILE      = "logs/status.json"

API_URL = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"


def send_message(text: str, chat_id=None):
    chat_id = chat_id or TELEGRAM_CHAT_ID
    requests.post(
        f"{API_URL}/sendMessage",
        json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"},
        timeout=10,
    )


def read_status():
    """Прочитать текущий статус, который пишет manager.py"""
    if not os.path.exists(STATUS_FILE):
        return None
    try:
        with open(STATUS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def format_status():
    data = read_status()
    if data is None:
        return "⚠️ Менеджер не запущен или ещё не создал статус-файл."

    active = data.get("active_bots", [])
    text = (
        f"📊 <b>Статус системы</b>\n\n"
        f"Активные боты: {len(active)}/{data.get('max_bots', '?')}\n"
        f"Монеты: {', '.join(active) or 'нет'}\n"
        f"PnL за день: {data.get('daily_pnl', 0):+.4f} USDT\n"
        f"Лимит потерь: {data.get('daily_loss_limit', '?')} USDT\n"
        f"RSI пороги: BUY &lt;{data.get('rsi_buy', '?')} / SELL &gt;{data.get('rsi_sell', '?')}\n"
        f"Обновлено: {data.get('updated_at', '?')}"
    )
    return text


def format_coins():
    data = read_status()
    if data is None:
        return "⚠️ Менеджер не запущен или ещё не создал статус-файл."

    coins = data.get("coins", {})
    if not coins:
        return "Нет данных по монетам (боты ещё не сделали первую проверку)."

    lines = ["🪙 <b>Детали по монетам</b>\n"]
    for symbol, info in coins.items():
        pnl  = info.get("pnl", 0)
        sign = "+" if pnl >= 0 else ""
        mode = info.get("mode", "?")
        mode_emoji = "↔️" if mode == "sideways" else "📈"
        lines.append(
            f"<b>{symbol}</b> {mode_emoji} {mode}\n"
            f"  Цена: {info.get('price')}\n"
            f"  RSI: {info.get('rsi')}\n"
            f"  ATR%: {info.get('atr_pct')}%\n"
            f"  Позиция: {info.get('position')}\n"
            f"  PnL: {sign}{pnl:.4f} USDT\n"
        )
    return "\n".join(lines)


def format_pnl():
    data = read_status()
    if data is None:
        return "⚠️ Менеджер не запущен."
    pnl = data.get("daily_pnl", 0)
    limit = data.get("daily_loss_limit", "?")
    sign = "+" if pnl >= 0 else ""
    return f"💸 PnL за день: <b>{sign}{pnl:.4f} USDT</b>\nЛимит потерь: {limit} USDT"


def format_top():
    """Запустить живой скан рынка и вернуть топ-монеты прямо сейчас"""
    try:
        coins = scan(top_n=5)   # берём топ-5 для общего обзора, не только активные 3
    except Exception as e:
        return f"⚠️ Ошибка сканирования: {e}"

    if not coins:
        return "🔍 Подходящих монет сейчас не найдено — рынок спокойный."

    lines = ["🐸 <b>Топ мемкоины прямо сейчас</b>\n"]
    for i, c in enumerate(coins, 1):
        vol_m = c["volume_24h"] / 1_000_000
        lines.append(
            f"{i}. <b>{c['symbol']}</b>\n"
            f"   Цена: {c['price']:.6f}\n"
            f"   Изменение 24ч: ±{c['change_pct']}%\n"
            f"   Объём: {vol_m:.1f}M USDT\n"
            f"   RSI: {c['rsi']}\n"
        )
    return "\n".join(lines)


def format_help():
    return (
        "🤖 <b>Доступные команды</b>\n\n"
        "<b>Статус:</b>\n"
        "/status — общий статус системы\n"
        "/coins — детали по каждой монете\n"
        "/pnl — текущий PnL за день\n"
        "/top — свежий скан топ-монет\n\n"
        "<b>Аналитика:</b>\n"
        "/daily — отчёт за сегодня\n"
        "/weekly — отчёт за неделю\n"
        "/monthly — отчёт за месяц\n"
        "/quarterly — отчёт за квартал\n"
        "/yearly — отчёт за год\n"
        "/alltime — отчёт за всё время\n\n"
        "/help — этот список"
    )


def handle_update(update: dict):
    message = update.get("message", {})
    text = message.get("text", "").strip()
    chat_id = message.get("chat", {}).get("id")

    if not text or chat_id is None:
        return

    # Безопасность: отвечаем только настроенному chat_id
    if str(chat_id) != str(TELEGRAM_CHAT_ID):
        return

    if text == "/status":
        send_message(format_status(), chat_id)
    elif text == "/coins":
        send_message(format_coins(), chat_id)
    elif text == "/pnl":
        send_message(format_pnl(), chat_id)
    elif text == "/top":
        send_message("🔍 Сканирую рынок, секунду...", chat_id)
        send_message(format_top(), chat_id)
    elif text == "/daily":
        send_message(analytics.format_report(analytics.today(), "День"), chat_id)
    elif text == "/weekly":
        send_message(analytics.format_report(analytics.this_week(), "Неделя"), chat_id)
    elif text == "/monthly":
        send_message(analytics.format_report(analytics.this_month(), "Месяц"), chat_id)
    elif text == "/quarterly":
        send_message(analytics.format_report(analytics.this_quarter(), "Квартал"), chat_id)
    elif text == "/yearly":
        send_message(analytics.format_report(analytics.this_year(), "Год"), chat_id)
    elif text == "/alltime":
        send_message(analytics.format_report(analytics.all_time(), "Всё время"), chat_id)
    elif text == "/help" or text == "/start":
        send_message(format_help(), chat_id)
    else:
        send_message("Неизвестная команда. Используй /help", chat_id)


def main():
    print("🤖 Telegram command bot запущен. Жду команды...")
    offset = None

    while True:
        try:
            params = {"timeout": 30}
            if offset:
                params["offset"] = offset

            resp = requests.get(f"{API_URL}/getUpdates", params=params, timeout=35)
            updates = resp.json().get("result", [])

            for update in updates:
                offset = update["update_id"] + 1
                handle_update(update)

        except Exception as e:
            print(f"⚠️ Ошибка: {e}")
            time.sleep(5)


if __name__ == "__main__":
    main()
