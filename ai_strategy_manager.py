"""
╔═══════════════════════════════════════════════════════════╗
║      AI_STRATEGY_MANAGER.PY — Управление стратегией       ║
║                                                            ║
║  AI корректирует параметры на основе рыночных условий:    ║
║  • RSI пороги для входа/выхода                            ║
║  • Размер позиций (QTY_USDT)                              ║
║  • Количество активных ботов (MAX_BOTS)                   ║
║  • Интервал сканирования (SCAN_INTERVAL_MIN)              ║
║                                                            ║
║  Лимиты:                                                   ║
║  • Вызов не чаще 2 раз в день (для экономии токенов)      ║
║  • Hard limits не нарушаются (MAX_LEVERAGE, MAX_POSITIONS) ║
║  • Параметры обновляются только при сильном сдвиге рынка   ║
╚═══════════════════════════════════════════════════════════╝
"""

import os
import json
import time
import logging
from datetime import datetime, timedelta
from typing import Optional

log = logging.getLogger("ai_strategy_manager")

# ── Конфигурация ──────────────────────────────────────────
CACHE_FILE = "logs/ai_strategy_cache.json"
MAX_CALLS_PER_DAY = 2
CALL_COOLDOWN_HOURS = 12  # минимум 12 часов между вызовами

# Hard limits (не могут быть нарушены AI)
HARD_LIMITS = {
    "LEVERAGE": {"min": 1, "max": 2},
    "MAX_BOTS": {"min": 1, "max": 5},
    "QTY_USDT": {"min": 10, "max": 50},
    "RSI_BUY": {"min": 20, "max": 50},
    "RSI_SELL": {"min": 50, "max": 80},
    "SCAN_INTERVAL_MIN": {"min": 5, "max": 60},
}


class AIStrategyManager:
    """AI-агент для корректировки торговой стратегии"""

    def __init__(self, hermes_brain=None):
        """
        Args:
            hermes_brain: модуль hermes_brain.py (опционально)
        """
        self.brain = hermes_brain
        self.cache = self._load_cache()

    def _load_cache(self) -> dict:
        """Загрузить кеш последних вызовов AI"""
        if os.path.exists(CACHE_FILE):
            try:
                with open(CACHE_FILE, "r") as f:
                    return json.load(f)
            except Exception as e:
                log.warning(f"Failed to load cache: {e}")
        return {"last_calls": [], "adjustments": {}}

    def _save_cache(self):
        """Сохранить кеш"""
        try:
            os.makedirs(os.path.dirname(CACHE_FILE), exist_ok=True)
            with open(CACHE_FILE, "w") as f:
                json.dump(self.cache, f, indent=2)
        except Exception as e:
            log.error(f"Failed to save cache: {e}")

    def _can_call_ai(self) -> tuple[bool, str]:
        """Проверить, можно ли вызвать AI (не превышен лимит 2 раза/день)"""
        now = datetime.now()
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)

        # Очистить старые вызовы (старше суток)
        self.cache["last_calls"] = [
            ts for ts in self.cache["last_calls"]
            if datetime.fromisoformat(ts) > today_start
        ]

        # Проверка лимита вызовов за день
        calls_today = len(self.cache["last_calls"])
        if calls_today >= MAX_CALLS_PER_DAY:
            return False, f"AI limit reached: {calls_today}/{MAX_CALLS_PER_DAY} calls today"

        # Проверка cooldown (минимум 12ч между вызовами)
        if self.cache["last_calls"]:
            last_call = datetime.fromisoformat(self.cache["last_calls"][-1])
            time_since = (now - last_call).total_seconds() / 3600
            if time_since < CALL_COOLDOWN_HOURS:
                hours_left = CALL_COOLDOWN_HOURS - time_since
                return False, f"Cooldown active: {hours_left:.1f}h left"

        return True, "OK"

    def _register_call(self):
        """Зарегистрировать вызов AI"""
        self.cache["last_calls"].append(datetime.now().isoformat())
        self._save_cache()

    def _enforce_limits(self, params: dict) -> dict:
        """Применить hard limits к параметрам"""
        for key, value in params.items():
            if key in HARD_LIMITS:
                limits = HARD_LIMITS[key]
                if value < limits["min"]:
                    log.warning(f"{key}={value} below minimum {limits['min']}, clamping")
                    params[key] = limits["min"]
                elif value > limits["max"]:
                    log.warning(f"{key}={value} above maximum {limits['max']}, clamping")
                    params[key] = limits["max"]
        return params

    def adjust_strategy(
        self,
        current_params: dict,
        market_context: dict,
        force: bool = False
    ) -> tuple[bool, dict, str]:
        """
        Запросить у AI корректировку стратегии на основе рыночных условий.

        Args:
            current_params: текущие параметры {'RSI_BUY': 45, 'RSI_SELL': 55, ...}
            market_context: контекст рынка {'volatility': 0.02, 'trend': 'sideways', ...}
            force: пропустить проверку лимитов (для тестов)

        Returns:
            (adjusted, new_params, reason)
            - adjusted: True если параметры изменены
            - new_params: новые параметры (или current_params если не изменены)
            - reason: объяснение от AI
        """
        # 1. Проверка лимитов вызовов
        if not force:
            can_call, reason = self._can_call_ai()
            if not can_call:
                log.info(f"Skipping AI adjustment: {reason}")
                return False, current_params, reason

        # 2. Проверка наличия Hermes
        if not self.brain:
            log.warning("Hermes brain not available, skipping adjustment")
            return False, current_params, "Hermes brain not available"

        # 3. Формирование промпта для AI
        prompt = self._build_adjustment_prompt(current_params, market_context)

        # 4. Вызов LLM
        try:
            log.info("🧠 Requesting strategy adjustment from AI...")
            response = self.brain._call_llm(prompt, system_prompt="""
You are a trading strategy optimizer for a crypto scalping bot.
Analyze market conditions and suggest parameter adjustments to maximize profit while managing risk.
Respond ONLY with valid JSON in this exact format:
{
  "adjust": true/false,
  "params": {
    "RSI_BUY": 35,
    "RSI_SELL": 65,
    "QTY_USDT": 25.0,
    "MAX_BOTS": 3,
    "SCAN_INTERVAL_MIN": 15
  },
  "reason": "Market is volatile, widening RSI range and reducing scan interval"
}
""")

            if not response:
                raise ValueError("Empty response from AI")

            # 5. Парсинг JSON ответа
            data = self.brain._parse_json(response)
            if not data or "adjust" not in data:
                raise ValueError(f"Invalid JSON structure: {response[:200]}")

            # 6. Если AI не предлагает изменений — возвращаем текущие параметры
            if not data["adjust"]:
                reason = data.get("reason", "No adjustment needed")
                log.info(f"🧠 AI: {reason}")
                self._register_call()
                return False, current_params, reason

            # 7. Применяем hard limits к предложенным параметрам
            new_params = self._enforce_limits(data["params"])
            reason = data.get("reason", "Strategy adjusted")

            # 8. Сохраняем изменения в кеш
            self.cache["adjustments"][datetime.now().isoformat()] = {
                "old": current_params,
                "new": new_params,
                "reason": reason,
            }
            self._register_call()

            log.info(f"🧠 AI adjusted strategy: {reason}")
            log.info(f"   New params: {json.dumps(new_params, indent=2)}")

            return True, new_params, reason

        except Exception as e:
            log.error(f"AI adjustment failed: {e}")
            return False, current_params, f"AI error: {e}"

    def _build_adjustment_prompt(self, current_params: dict, market_context: dict) -> str:
        """Создать промпт для AI с текущими параметрами и контекстом рынка"""
        return f"""
Analyze the trading bot performance and suggest parameter adjustments.

Current Parameters:
{json.dumps(current_params, indent=2)}

Market Context:
{json.dumps(market_context, indent=2)}

Hard Limits (cannot be exceeded):
{json.dumps(HARD_LIMITS, indent=2)}

Goal: Maximize daily profit (+10-50 USDT/day) via scalping while managing risk.

Consider:
1. If winrate is low → tighten RSI range or reduce position size
2. If too few trades → widen RSI range or increase scan frequency
3. If high volatility → reduce leverage or position size
4. If sideways market → use Bollinger Bands strategy (narrower RSI)
5. If trending market → use RSI breakout strategy (wider RSI)

Current issues:
- Only 2 trades/week (target: 5-10 trades/day for scalping)
- RSI 45/55 too conservative
- QTY_USDT 20 → should be 25-35 for meaningful profit

Suggest adjustments respecting hard limits.
"""

    def get_adjustment_history(self, limit: int = 10) -> list[dict]:
        """Получить историю корректировок AI"""
        adjustments = self.cache.get("adjustments", {})
        items = sorted(adjustments.items(), key=lambda x: x[0], reverse=True)
        return [{"timestamp": ts, **data} for ts, data in items[:limit]]

    def get_status(self) -> dict:
        """Получить статус AI Strategy Manager"""
        now = datetime.now()
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)

        calls_today = [
            ts for ts in self.cache.get("last_calls", [])
            if datetime.fromisoformat(ts) > today_start
        ]

        return {
            "calls_today": len(calls_today),
            "max_calls_per_day": MAX_CALLS_PER_DAY,
            "remaining_calls": MAX_CALLS_PER_DAY - len(calls_today),
            "last_call": calls_today[-1] if calls_today else None,
            "cooldown_hours": CALL_COOLDOWN_HOURS,
            "total_adjustments": len(self.cache.get("adjustments", {})),
        }


# ── Экспорт ───────────────────────────────────────────────

def create_manager(hermes_brain=None) -> AIStrategyManager:
    """Factory функция для создания AI Strategy Manager"""
    return AIStrategyManager(hermes_brain)
