"""
╔══════════════════════════════════════════════╗
║      TELEGRAM_NOTIFIER.PY — Уведомления     ║
║   Модуль для отправки сообщений в Telegram   ║
╚══════════════════════════════════════════════╝

Импортируется в manager.py для отправки уведомлений
о сделках, ошибках и статусе ботов.
"""

import os
import requests
import logging

log = logging.getLogger("telegram")

TELEGRAM_TOKEN   = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

API_URL = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"


def send_message(text: str):
    """Отправить сообщение в Telegram. Не падает при ошибке сети."""
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return  # уведомления не настроены — просто пропускаем

    try:
        requests.post(
            f"{API_URL}/sendMessage",
            json={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": text,
                "parse_mode": "HTML",
            },
            timeout=10,
        )
    except Exception as e:
        log.warning(f"Не удалось отправить Telegram-сообщение: {e}")


def notify_bot_started(symbol: str):
    send_message(f"▶️ Запущен бот: <b>{symbol}</b>")


def notify_bot_stopped(symbol: str):
    send_message(f"⏹ Остановлен бот: <b>{symbol}</b>")


def notify_position_opened(symbol: str, side: str, qty: str, price: float, tp: float, sl: float):
    emoji = "🟢" if side == "Buy" else "🔴"
    label = "LONG" if side == "Buy" else "SHORT"
    send_message(
        f"{emoji} <b>{label}</b> {symbol}\n"
        f"Цена входа: {price}\n"
        f"Объём: {qty}\n"
        f"🎯 TP: {tp}\n"
        f"🛑 SL: {sl}"
    )


def notify_position_closed(symbol: str, side: str, pnl: float):
    emoji = "✅" if pnl >= 0 else "❌"
    label = "LONG" if side == "Buy" else "SHORT"
    sign = "+" if pnl >= 0 else ""
    send_message(
        f"{emoji} Закрыта позиция <b>{label}</b> {symbol}\n"
        f"PnL: {sign}{pnl:.4f} USDT"
    )


def notify_error(symbol: str, error: str):
    send_message(f"⚠️ Ошибка [{symbol}]: {error}")


def notify_daily_limit(symbol: str):
    send_message(f"⛔ <b>Дневной лимит потерь достигнут!</b>\nБот {symbol} остановлен.")


def notify_manager_started(max_bots: int, scan_interval: int):
    send_message(
        f"🚀 <b>Менеджер запущен</b>\n"
        f"Макс ботов: {max_bots}\n"
        f"Сканирование каждые {scan_interval} мин"
    )


def notify_manager_stopped():
    send_message("🔴 <b>Менеджер остановлен</b>\nВсе боты выключены.")
