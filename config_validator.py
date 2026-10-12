"""
╔════════════════════════════════════════════════════════════╗
║        CONFIG_VALIDATOR.PY v2 — Валидация конфигурации     ║
║                                                             ║
║  Fixes v2:                                                 ║
║  • Removed STOP_LOSS_PCT/TAKE_PROFIT_PCT (not in .env)    ║
║  • health_check hits /v1/models (not /health)             ║
║  • Added HERMES_API_KEY warning                            ║
║  • Checks Authorization header on /v1/models              ║
╚════════════════════════════════════════════════════════════╝
"""

import os
import sys
import logging
import requests
from typing import Tuple

log = logging.getLogger("config_validator")


class ConfigError(Exception):
    pass


class ConfigValidator:
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
        self.errors   = []
        self.warnings = []

    def validate_all(self) -> Tuple[bool, list, list]:
        self.errors = []; self.warnings = []
        self._check_required_vars()
        self._check_trading_params()
        self._check_safety_limits()
        self._check_hermes()
        self._check_telegram()
        return (len(self.errors) == 0, self.errors, self.warnings)

    def _check_required_vars(self):
        for var in self.REQUIRED_VARS:
            if not os.getenv(var):
                self.errors.append(f"❌ Missing required variable: {var}")

    def _check_trading_params(self):
        try:
            qty     = float(os.getenv("QTY_USDT",          "0"))
            min_qty = float(os.getenv("MIN_POSITION_USDT", "10"))
            max_qty = float(os.getenv("MAX_POSITION_USDT", "30"))
            if qty < 1:
                self.errors.append("❌ QTY_USDT must be >= 1")
            if not (min_qty <= qty <= max_qty):
                self.errors.append(f"❌ QTY_USDT={qty} outside [{min_qty}, {max_qty}]")
        except ValueError as e:
            self.errors.append(f"❌ QTY_USDT: {e}")

        try:
            max_bots = int(os.getenv("MAX_BOTS", "3"))
            if not (1 <= max_bots <= 10):
                self.errors.append("❌ MAX_BOTS must be in [1, 10]")
        except ValueError as e:
            self.errors.append(f"❌ MAX_BOTS: {e}")

        try:
            lev     = int(os.getenv("LEVERAGE",     "2"))
            max_lev = int(os.getenv("MAX_LEVERAGE", "2"))
            if not (1 <= lev <= 10):
                self.errors.append("❌ LEVERAGE must be in [1, 10]")
            if lev > max_lev:
                self.errors.append(f"❌ LEVERAGE ({lev}) > MAX_LEVERAGE ({max_lev})")
        except ValueError as e:
            self.errors.append(f"❌ LEVERAGE: {e}")

        # FIX: removed STOP_LOSS_PCT / TAKE_PROFIT_PCT checks — not used in bot
        interval = os.getenv("INTERVAL", "5")
        valid_intervals = ("1", "3", "5", "15", "30", "60", "120", "240", "D")
        if interval not in valid_intervals:
            self.warnings.append(f"⚠️ INTERVAL={interval} is non-standard. Valid: {', '.join(valid_intervals)}")

    def _check_safety_limits(self):
        try:
            limit = float(os.getenv("DAILY_LOSS_LIMIT", "-20"))
            if limit >= 0:
                self.errors.append("❌ DAILY_LOSS_LIMIT must be negative (e.g. -20)")
            if limit < -1000:
                self.warnings.append(f"⚠️ DAILY_LOSS_LIMIT={limit} is very large")
        except ValueError as e:
            self.errors.append(f"❌ DAILY_LOSS_LIMIT: {e}")

    def _check_hermes(self):
        hermes_enabled = os.getenv("HERMES_ENABLED", "0") == "1"
        if not hermes_enabled:
            self.warnings.append("⚠️ Hermes AI disabled (HERMES_ENABLED=0)")
            return

        url = os.getenv("HERMES_API_URL", "")
        key = os.getenv("HERMES_API_KEY", "")
        if not url:
            self.errors.append("❌ HERMES_ENABLED=1 but HERMES_API_URL not set")
            return
        # FIX: warn if API key missing
        if not key:
            self.warnings.append("⚠️ HERMES_API_KEY is empty — requests may be rejected")

        ai_mode = os.getenv("AI_MODE", "veto_only")
        if ai_mode not in ("veto_only", "hybrid", "autonomous"):
            self.errors.append(f"❌ AI_MODE='{ai_mode}' invalid. Valid: veto_only, hybrid, autonomous")

        # FIX: ping /v1/models not /health — Hermes doesn't have /health
        try:
            models_url = url.rsplit("/chat/completions", 1)[0] + "/models"
            headers    = {}
            if key:
                headers["Authorization"] = f"Bearer {key}"
            resp = requests.get(models_url, headers=headers, timeout=5)
            if resp.status_code != 200:
                self.warnings.append(f"⚠️ Hermes API ({models_url}) returned {resp.status_code}")
        except requests.exceptions.ConnectionError:
            self.warnings.append(f"⚠️ Cannot reach Hermes at {url}")
        except Exception as e:
            self.warnings.append(f"⚠️ Hermes check error: {e}")

    def _check_telegram(self):
        token   = os.getenv("TELEGRAM_TOKEN",   "")
        chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
        if not token or len(token) < 20:
            self.errors.append("❌ TELEGRAM_TOKEN is invalid")
        if not chat_id or not chat_id.strip().lstrip("-").isdigit():
            self.errors.append("❌ TELEGRAM_CHAT_ID must be a number")
        if token and len(token) > 20:
            try:
                resp = requests.get(f"https://api.telegram.org/bot{token}/getMe", timeout=5)
                if resp.status_code != 200:
                    self.warnings.append("⚠️ Telegram token invalid or bot blocked")
            except Exception:
                pass

    def print_results(self, errors, warnings):
        if errors:
            print("\n" + "="*60)
            print("❌ ERRORS:")
            for e in errors: print(f"  {e}")
        if warnings:
            print("\n" + "="*60)
            print("⚠️  WARNINGS:")
            for w in warnings: print(f"  {w}")
        if not errors and not warnings:
            print("\n✅ Configuration is valid.")


def validate_config() -> bool:
    v  = ConfigValidator()
    ok, errors, warnings = v.validate_all()
    v.print_results(errors, warnings)
    return ok


if __name__ == "__main__":
    from dotenv import load_dotenv
    env_file = sys.argv[1] if len(sys.argv) > 1 else ".env"
    if not os.path.exists(env_file):
        print(f"❌ File {env_file} not found")
        sys.exit(1)
    print(f"📋 Validating {env_file}...")
    load_dotenv(env_file)
    sys.exit(0 if validate_config() else 1)
