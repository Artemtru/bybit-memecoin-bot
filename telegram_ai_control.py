"""
╔════════════════════════════════════════════════════════════╗
║        TELEGRAM_AI_CONTROL.PY — AI команды через /ask      ║
║                                                             ║
║  Обработка команд пользователя в свободной форме:          ║
║  /ask Закрой все позиции                                    ║
║  /ask Увеличь размер позиции до 30 USDT                     ║
║  /ask Какие монеты сейчас перспективны?                     ║
║                                                             ║
║  AI агент (Hermes) интерпретирует команду и выполняет      ║
║  через API бота с валидацией лимитов безопасности.         ║
╚════════════════════════════════════════════════════════════╝
"""

import os
import json
import logging
import requests
from typing import Dict, Any, Optional

log = logging.getLogger("ai_control")

# ─────────────────────────────────────────────────────────────
#  Конфигурация
# ─────────────────────────────────────────────────────────────

HERMES_API_URL = os.getenv(
    "HERMES_API_URL",
    "http://localhost:8642/v1/chat/completions",
)
HERMES_MODEL = os.getenv("HERMES_MODEL", "anthropic/claude-sonnet-4.5")
HERMES_TIMEOUT = int(os.getenv("HERMES_TIMEOUT", "30"))

# Жёсткие границы безопасности (AI не может их нарушить)
HARD_LIMITS = {
    "max_position_usdt": float(os.getenv("MAX_POSITION_USDT", "30")),
    "min_position_usdt": float(os.getenv("MIN_POSITION_USDT", "10")),
    "max_daily_loss": float(os.getenv("DAILY_LOSS_LIMIT", "-20")),
    "max_positions": int(os.getenv("MAX_BOTS", "5")),
    "max_leverage": int(os.getenv("LEVERAGE", "2")),
}

# ═════════════════════════════════════════════════════════════
#  Системный промпт для AI агента
# ═════════════════════════════════════════════════════════════

SYSTEM_PROMPT = f"""You are an AI trading assistant controlling a memecoin futures bot on Bybit.
The user gives you commands in natural language (Russian or English), and you execute them via bot API.

HARD LIMITS (you CANNOT violate these):
- Position size: {HARD_LIMITS['min_position_usdt']}-{HARD_LIMITS['max_position_usdt']} USDT
- Max positions: {HARD_LIMITS['max_positions']}
- Max leverage: {HARD_LIMITS['max_leverage']}x
- Daily loss limit: {HARD_LIMITS['max_daily_loss']} USDT

USER COMMANDS EXAMPLES:
- "Закрой все позиции" → close_all_positions()
- "Увеличь размер позиции до 25 USDT" → set_position_size(25)
- "Какие монеты сейчас перспективны?" → analyze_market()
- "Открой лонг DOGE" → suggest_trade("DOGE", "long")
- "Стоп бот на час" → pause_bot(60)

RESPONSE FORMAT:
{{
  "action": "close_all" | "set_param" | "analyze" | "suggest_trade" | "pause" | "resume" | "status",
  "params": {{}},  // parameters for the action
  "needs_confirmation": true/false,  // require user approval?
  "explanation": "Brief explanation in Russian"
}}

SAFETY:
- Operations that LOSE money or CLOSE positions require confirmation.
- Operations that just change parameters (<100 USDT impact) can auto-execute.
- Always validate against HARD_LIMITS before returning.
"""


# ═════════════════════════════════════════════════════════════
#  Внутренние функции
# ═════════════════════════════════════════════════════════════

def _call_llm(user_message: str, bot_state: Dict[str, Any]) -> Optional[dict]:
    """
    Отправить команду пользователя в Hermes и получить структурированный ответ.

    Аргументы:
        user_message: команда пользователя в свободной форме
        bot_state:    текущее состояние бота (позиции, PnL, параметры)

    Возвращает:
        dict с action и params, или None при ошибке
    """
    # Добавляем состояние бота в контекст
    context = (
        f"\n\nCURRENT BOT STATE:\n"
        f"- Open positions: {bot_state.get('positions', [])}\n"
        f"- Daily PnL: {bot_state.get('daily_pnl', 0):.2f} USDT\n"
        f"- Position size: {bot_state.get('position_size', 20)} USDT\n"
        f"- Max positions: {bot_state.get('max_positions', 3)}\n"
        f"- Bot status: {bot_state.get('status', 'running')}\n"
    )

    user_prompt = f"User command: {user_message}{context}"

    payload = {
        "model": HERMES_MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": 0.2,
        "max_tokens": 512,
    }

    log.info(f"[ai_control] → User command: {user_message}")

    try:
        resp = requests.post(
            HERMES_API_URL,
            json=payload,
            timeout=HERMES_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()

        content = data["choices"][0]["message"]["content"]
        log.info(f"[ai_control] ← AI response: {content[:200]}...")

        # Парсим JSON из ответа
        parsed = _parse_json(content)
        return parsed

    except requests.exceptions.Timeout:
        log.warning(f"[ai_control] Timeout при обращении к Hermes")
        return None

    except requests.exceptions.ConnectionError:
        log.warning("[ai_control] Hermes недоступен")
        return None

    except Exception as e:
        log.error(f"[ai_control] Ошибка: {e}", exc_info=True)
        return None


def _parse_json(text: str) -> Optional[dict]:
    """
    Извлечь JSON из ответа LLM (обрабатывает markdown обёртки).
    """
    if not text:
        return None

    cleaned = text.strip()
    if cleaned.startswith("```"):
        lines = cleaned.split("\n")
        if lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        cleaned = "\n".join(lines)

    try:
        return json.loads(cleaned)
    except json.JSONDecodeError as e:
        log.warning(f"[ai_control] Не удалось распарсить JSON: {e}")
        return None


def _validate_action(action_data: dict) -> tuple[bool, str]:
    """
    Валидация действия против HARD_LIMITS.

    Возвращает:
        (valid: bool, error_message: str)
    """
    action = action_data.get("action")
    params = action_data.get("params", {})

    # Проверка position_size
    if action == "set_param" and "position_size" in params:
        size = float(params["position_size"])
        if not (HARD_LIMITS["min_position_usdt"] <= size <= HARD_LIMITS["max_position_usdt"]):
            return (
                False,
                f"❌ Размер позиции {size} USDT нарушает лимиты "
                f"({HARD_LIMITS['min_position_usdt']}-{HARD_LIMITS['max_position_usdt']} USDT)",
            )

    # Проверка max_positions
    if action == "set_param" and "max_positions" in params:
        count = int(params["max_positions"])
        if count > HARD_LIMITS["max_positions"]:
            return (
                False,
                f"❌ Макс. позиций {count} превышает лимит {HARD_LIMITS['max_positions']}",
            )

    # Проверка leverage
    if action == "set_param" and "leverage" in params:
        lev = int(params["leverage"])
        if lev > HARD_LIMITS["max_leverage"]:
            return (
                False,
                f"❌ Плечо {lev}x превышает лимит {HARD_LIMITS['max_leverage']}x",
            )

    return (True, "")


# ═════════════════════════════════════════════════════════════
#  Публичные функции
# ═════════════════════════════════════════════════════════════

def process_command(
    user_message: str,
    bot_state: Dict[str, Any],
) -> Dict[str, Any]:
    """
    Обработать команду пользователя через AI агента.

    Аргументы:
        user_message: команда в свободной форме
        bot_state:    текущее состояние бота

    Возвращает:
        dict {
            "success": bool,
            "action": str,
            "params": dict,
            "needs_confirmation": bool,
            "explanation": str,
            "error": str (только если success=False)
        }
    """
    # Вызываем LLM
    response = _call_llm(user_message, bot_state)

    if response is None:
        return {
            "success": False,
            "error": "AI агент недоступен. Попробуйте позже.",
        }

    # Валидация
    valid, error_msg = _validate_action(response)
    if not valid:
        return {
            "success": False,
            "error": error_msg,
        }

    # Всё OK
    return {
        "success": True,
        "action": response.get("action", "unknown"),
        "params": response.get("params", {}),
        "needs_confirmation": response.get("needs_confirmation", False),
        "explanation": response.get("explanation", "Действие обработано"),
    }


def execute_action(
    action: str,
    params: Dict[str, Any],
    bot_manager,  # ссылка на основной Manager бота
) -> tuple[bool, str]:
    """
    Выполнить действие через bot manager API.

    Аргументы:
        action:       тип действия (close_all, set_param, etc.)
        params:       параметры действия
        bot_manager:  экземпляр основного Manager класса

    Возвращает:
        (success: bool, message: str)
    """
    try:
        if action == "close_all":
            count = bot_manager.close_all_positions()
            return (True, f"✅ Закрыто позиций: {count}")

        elif action == "set_param":
            param_name = list(params.keys())[0]
            param_value = params[param_name]
            bot_manager.set_parameter(param_name, param_value)
            return (True, f"✅ Параметр {param_name} установлен: {param_value}")

        elif action == "pause":
            minutes = params.get("minutes", 60)
            bot_manager.pause(minutes)
            return (True, f"⏸️ Бот приостановлен на {minutes} минут")

        elif action == "resume":
            bot_manager.resume()
            return (True, "▶️ Бот возобновлён")

        elif action == "analyze":
            analysis = bot_manager.get_market_analysis()
            return (True, f"📊 Анализ рынка:\n{analysis}")

        elif action == "suggest_trade":
            symbol = params.get("symbol", "")
            direction = params.get("direction", "long")
            bot_manager.queue_manual_trade(symbol, direction)
            return (True, f"✅ Сделка добавлена в очередь: {direction.upper()} {symbol}")

        elif action == "status":
            status = bot_manager.get_status_text()
            return (True, status)

        else:
            return (False, f"❌ Неизвестное действие: {action}")

    except Exception as e:
        log.error(f"[ai_control] Ошибка выполнения {action}: {e}", exc_info=True)
        return (False, f"❌ Ошибка выполнения: {str(e)}")


# ─────────────────────────────────────────────────────────────
#  Для тестирования из командной строки
# ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.DEBUG,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )

    print("=" * 60)
    print("Telegram AI Control — тест модуля")
    print("=" * 60)

    # Мокаем состояние бота
    mock_state = {
        "positions": [
            {"symbol": "DOGEUSDT", "side": "long", "size": 20, "pnl": 2.5}
        ],
        "daily_pnl": 3.45,
        "position_size": 20,
        "max_positions": 3,
        "status": "running",
    }

    # Тестовые команды
    test_commands = [
        "Закрой все позиции",
        "Увеличь размер позиции до 25 USDT",
        "Какие монеты сейчас перспективны?",
        "Открой лонг DOGE",
    ]

    for cmd in test_commands:
        print(f"\n📝 Команда: {cmd}")
        result = process_command(cmd, mock_state)
        print(f"Результат: {json.dumps(result, indent=2, ensure_ascii=False)}")
