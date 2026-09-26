"""
╔════════════════════════════════════════════════════════════╗
║        CONFIG_VALIDATOR.PY — Валидация конфигурации        ║
║                                                             ║
║  Проверка параметров .env при старте бота:                 ║
║  - Обязательные переменные присутствуют                    ║
║  - Значения в допустимых диапазонах                        ║
║  - Лимиты безопасности корректны                           ║
║  - Hermes API доступен (если HERMES_ENABLED=1)             ║
╚════════════════════════════════════════════════════════════╝
"""

import os
import sys
import logging
from typing import Optional, Tuple
import requests

log = logging.getLogger("config_validator")


class ConfigError(Exception):
    """Ошибка в конфигурации бота"""
    pass


class ConfigValidator:
    """Валидатор конфигурации бота"""

    REQUIRED_VARS = [
        "BYBIT_API_KEY",
        "BYBIT_API_SECRET",
        "TELEGRAM_TOKEN",
        "TELEGRAM_CHAT_ID",
        "QTY_USDT",
        "MAX_BOTS",
        "LEVERAGE",
        "DAILY_LOSS_LIMIT",
    ]

    def __init__(self):
        self.errors = []
        self.warnings = []

    def validate_all(self) -> Tuple[bool, list, list]:
        """
        Валидация всех параметров конфигурации.

        Возвращает:
            (is_valid, errors, warnings)
        """
        self.errors = []
        self.warnings = []

        # 1. Обязательные переменные
        self._check_required_vars()

        # 2. Trading параметры
        self._check_trading_params()

        # 3. Лимиты безопасности
        self._check_safety_limits()

        # 4. Hermes AI (если включён)
        self._check_hermes()

        # 5. Telegram
        self._check_telegram()

        is_valid = len(self.errors) == 0
        return (is_valid, self.errors, self.warnings)

    def _check_required_vars(self):
        """Проверка наличия обязательных переменных"""
        for var in self.REQUIRED_VARS:
            if not os.getenv(var):
                self.errors.append(f"❌ Отсутствует обязательная переменная: {var}")

    def _check_trading_params(self):
        """Проверка торговых параметров"""
        try:
            qty = float(os.getenv("QTY_USDT", "0"))
            min_qty = float(os.getenv("MIN_POSITION_USDT", "10"))
            max_qty = float(os.getenv("MAX_POSITION_USDT", "30"))

            if qty < 1:
                self.errors.append("❌ QTY_USDT должен быть >= 1")
            
            if not (min_qty <= qty <= max_qty):
                self.errors.append(
                    f"❌ QTY_USDT={qty} выходит за диапазон "
                    f"[{min_qty}, {max_qty}]"
                )

            if min_qty >= max_qty:
                self.errors.append(
                    f"❌ MIN_POSITION_USDT ({min_qty}) должен быть < "
                    f"MAX_POSITION_USDT ({max_qty})"
                )

        except ValueError as e:
            self.errors.append(f"❌ Некорректное значение QTY/MIN/MAX: {e}")

        # Максимум позиций
        try:
            max_bots = int(os.getenv("MAX_BOTS", "3"))
            max_positions = int(os.getenv("MAX_POSITIONS", "5"))

            if max_bots < 1 or max_bots > 10:
                self.errors.append("❌ MAX_BOTS должен быть в диапазоне [1, 10]")

            if max_positions < max_bots:
                self.errors.append(
                    f"❌ MAX_POSITIONS ({max_positions}) должен быть >= "
                    f"MAX_BOTS ({max_bots})"
                )

        except ValueError as e:
            self.errors.append(f"❌ Некорректное значение MAX_BOTS/MAX_POSITIONS: {e}")

        # Плечо
        try:
            leverage = int(os.getenv("LEVERAGE", "2"))
            max_leverage = int(os.getenv("MAX_LEVERAGE", "2"))

            if leverage < 1 or leverage > 10:
                self.errors.append("❌ LEVERAGE должен быть в диапазоне [1, 10]")

            if leverage > max_leverage:
                self.errors.append(
                    f"❌ LEVERAGE ({leverage}) превышает "
                    f"MAX_LEVERAGE ({max_leverage})"
                )

        except ValueError as e:
            self.errors.append(f"❌ Некорректное значение LEVERAGE: {e}")

    def _check_safety_limits(self):
        """Проверка лимитов безопасности"""
        try:
            loss_limit = float(os.getenv("DAILY_LOSS_LIMIT", "-20"))
            
            if loss_limit >= 0:
                self.errors.append(
                    "❌ DAILY_LOSS_LIMIT должен быть отрицательным (например, -20)"
                )

            if loss_limit < -1000:
                self.warnings.append(
                    f"⚠️ DAILY_LOSS_LIMIT={loss_limit} слишком большой. "
                    "Рекомендуется -50 или выше"
                )

        except ValueError as e:
            self.errors.append(f"❌ Некорректное значение DAILY_LOSS_LIMIT: {e}")

        # Stop-loss / Take-profit
        try:
            sl = float(os.getenv("STOP_LOSS_PCT", "5.0"))
            tp = float(os.getenv("TAKE_PROFIT_PCT", "8.0"))

            if sl <= 0 or sl > 20:
                self.errors.append("❌ STOP_LOSS_PCT должен быть в диапазоне (0, 20]")

            if tp <= 0 or tp > 50:
                self.errors.append("❌ TAKE_PROFIT_PCT должен быть в диапазоне (0, 50]")

            if tp <= sl:
                self.errors.append(
                    f"❌ TAKE_PROFIT_PCT ({tp}) должен быть > "
                    f"STOP_LOSS_PCT ({sl})"
                )

        except ValueError as e:
            self.errors.append(f"❌ Некорректное значение SL/TP: {e}")

    def _check_hermes(self):
        """Проверка Hermes AI интеграции"""
        hermes_enabled = os.getenv("HERMES_ENABLED", "0") == "1"
        
        if not hermes_enabled:
            self.warnings.append("⚠️ Hermes AI отключён (HERMES_ENABLED=0)")
            return

        # Проверяем наличие URL
        hermes_url = os.getenv("HERMES_API_URL", "")
        if not hermes_url:
            self.errors.append("❌ HERMES_ENABLED=1 но HERMES_API_URL не задан")
            return

        # Проверяем AI_MODE
        ai_mode = os.getenv("AI_MODE", "veto_only")
        valid_modes = ["veto_only", "hybrid", "autonomous"]
        if ai_mode not in valid_modes:
            self.errors.append(
                f"❌ AI_MODE='{ai_mode}' некорректен. "
                f"Допустимые: {', '.join(valid_modes)}"
            )

        # Пингуем Hermes API
        try:
            resp = requests.get(
                hermes_url.replace("/v1/chat/completions", "/health"),
                timeout=5
            )
            if resp.status_code != 200:
                self.warnings.append(
                    f"⚠️ Hermes API ({hermes_url}) не отвечает (status {resp.status_code})"
                )
        except requests.exceptions.ConnectionError:
            self.warnings.append(
                f"⚠️ Не удалось подключиться к Hermes API ({hermes_url}). "
                "Убедитесь что сервер запущен."
            )
        except Exception as e:
            self.warnings.append(f"⚠️ Ошибка при проверке Hermes API: {e}")

    def _check_telegram(self):
        """Проверка Telegram настроек"""
        token = os.getenv("TELEGRAM_TOKEN", "")
        chat_id = os.getenv("TELEGRAM_CHAT_ID", "")

        if not token or len(token) < 20:
            self.errors.append("❌ TELEGRAM_TOKEN некорректен")

        if not chat_id or not chat_id.strip().lstrip("-").isdigit():
            self.errors.append("❌ TELEGRAM_CHAT_ID должен быть числом")

        # Проверяем связь с Telegram (но не блокируем если ошибка)
        if token and len(token) > 20:
            try:
                resp = requests.get(
                    f"https://api.telegram.org/bot{token}/getMe",
                    timeout=5
                )
                if resp.status_code != 200:
                    self.warnings.append(
                        "⚠️ Telegram токен некорректен или бот заблокирован"
                    )
            except Exception:
                # Сетевые ошибки не критичны
                pass

    def print_results(self, errors: list, warnings: list):
        """Вывести результаты валидации"""
        if errors:
            print("\n" + "="*60)
            print("❌ ОШИБКИ КОНФИГУРАЦИИ:")
            print("="*60)
            for error in errors:
                print(f"  {error}")
            print()

        if warnings:
            print("\n" + "="*60)
            print("⚠️  ПРЕДУПРЕЖДЕНИЯ:")
            print("="*60)
            for warning in warnings:
                print(f"  {warning}")
            print()

        if not errors and not warnings:
            print("\n✅ Конфигурация корректна. Все проверки пройдены.")


def validate_config() -> bool:
    """
    Валидация конфигурации. Вызывать при старте бота.

    Возвращает:
        True если конфигурация валидна, False если есть критичные ошибки
    """
    validator = ConfigValidator()
    is_valid, errors, warnings = validator.validate_all()
    validator.print_results(errors, warnings)
    return is_valid


# ─────────────────────────────────────────────────────────────
#  CLI для ручной проверки
# ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    from dotenv import load_dotenv
    
    # Загружаем .env (или .env.production)
    env_file = sys.argv[1] if len(sys.argv) > 1 else ".env"
    
    if not os.path.exists(env_file):
        print(f"❌ Файл {env_file} не найден")
        sys.exit(1)
    
    print(f"📋 Проверяю конфигурацию из {env_file}...")
    load_dotenv(env_file)
    
    is_valid = validate_config()
    
    if is_valid:
        print("\n✅ Всё готово для запуска бота.")
        sys.exit(0)
    else:
        print("\n❌ Исправьте ошибки перед запуском.")
        sys.exit(1)
