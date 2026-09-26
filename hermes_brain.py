"""
╔══════════════════════════════════════════════════════╗
║        HERMES_BRAIN.PY — LLM-интеграция             ║
║                                                      ║
║  Подключение к Hermes API (OpenAI-совместимый        ║
║  шлюз) для интеллектуальной оценки сделок:          ║
║  • Финальная проверка перед входом в сделку          ║
║  • Оценка общего настроения рынка                    ║
║  • Проверка rug-pull риска                           ║
║  • Генерация объяснений для Telegram                 ║
║  • Ежедневная сводка по результатам                  ║
╚══════════════════════════════════════════════════════╝

Hermes gateway запущен в Docker на том же VPS.
API совместим с OpenAI Chat Completions.
При отключении или недоступности Hermes — все функции
возвращают нейтральные/разрешающие значения, чтобы
бот продолжал работать автономно.
"""

import os
import json
import time
import logging
import requests

log = logging.getLogger("hermes_brain")

# ── Конфигурация ──────────────────────────────────────

HERMES_API_URL = os.getenv(
    "HERMES_API_URL",
    "http://localhost:8642/v1/chat/completions",
)
HERMES_ENABLED = os.getenv("HERMES_ENABLED", "0") == "1"
HERMES_MODEL = os.getenv("HERMES_MODEL", "anthropic/claude-sonnet-4.5")
HERMES_TIMEOUT = int(os.getenv("HERMES_TIMEOUT", "30"))

# Rate limit: не чаще 1 вызова в 30 секунд
RATE_LIMIT_SEC = 30
_last_call_ts = 0.0

# ── Системный промпт (общий для всех функций) ────────

SYSTEM_PROMPT = (
    "You are a memecoin trading advisor for an automated bot on Bybit futures. "
    "The bot uses RSI + Bollinger Bands + ATR with ADX trend filter and volume "
    "confirmation on 15-min candles. Your role is to provide a final safety check "
    "before trades and assess overall market conditions. Be concise — respond "
    "with JSON only."
)


# ══════════════════════════════════════════════════════
#  Внутренние утилиты
# ══════════════════════════════════════════════════════

def _is_enabled() -> bool:
    """Проверка: включён ли Hermes."""
    return HERMES_ENABLED


def _rate_limited() -> bool:
    """
    Проверка rate-limit.
    Возвращает True если последний вызов был менее RATE_LIMIT_SEC секунд назад.
    """
    global _last_call_ts
    now = time.time()
    if now - _last_call_ts < RATE_LIMIT_SEC:
        wait = RATE_LIMIT_SEC - (now - _last_call_ts)
        log.debug(f"[hermes] Rate limit: следующий вызов через {wait:.0f}с")
        return True
    return False


def _call_llm(user_prompt: str, system_prompt: str = SYSTEM_PROMPT) -> str | None:
    """
    Отправить запрос в Hermes API и вернуть текст ответа.

    Аргументы:
        user_prompt:   конкретный запрос с данными
        system_prompt: системный промпт (контекст роли)

    Возвращает:
        Текст ответа LLM или None при ошибке/rate-limit/отключении.
    """
    global _last_call_ts

    if not _is_enabled():
        log.debug("[hermes] Hermes отключён (HERMES_ENABLED=0)")
        return None

    if _rate_limited():
        log.info("[hermes] Rate limit — пропускаем вызов")
        return None

    payload = {
        "model": HERMES_MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": 0.3,
        "max_tokens": 512,
    }

    log.info(f"[hermes] → LLM запрос: {user_prompt[:200]}...")

    try:
        _last_call_ts = time.time()
        resp = requests.post(
            HERMES_API_URL,
            json=payload,
            timeout=HERMES_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()

        # OpenAI-формат: choices[0].message.content
        content = data["choices"][0]["message"]["content"]
        log.info(f"[hermes] ← LLM ответ: {content[:300]}")
        return content

    except requests.exceptions.Timeout:
        log.warning(f"[hermes] Таймаут ({HERMES_TIMEOUT}с) при обращении к Hermes")
        return None

    except requests.exceptions.ConnectionError:
        log.warning("[hermes] Hermes недоступен (ConnectionError)")
        return None

    except requests.exceptions.HTTPError as e:
        log.warning(f"[hermes] HTTP ошибка: {e}")
        return None

    except (KeyError, IndexError, json.JSONDecodeError) as e:
        log.warning(f"[hermes] Некорректный ответ от LLM: {e}")
        return None

    except Exception as e:
        log.error(f"[hermes] Непредвиденная ошибка: {e}", exc_info=True)
        return None


def _parse_json(text: str | None) -> dict | None:
    """
    Извлечь JSON из ответа LLM.
    Обрабатывает случаи, когда LLM оборачивает JSON в markdown ```json ... ```.

    Возвращает:
        dict или None при ошибке парсинга.
    """
    if text is None:
        return None

    # Убрать markdown-обёртку если есть
    cleaned = text.strip()
    if cleaned.startswith("```"):
        # Удалить первую строку (```json) и последнюю (```)
        lines = cleaned.split("\n")
        if lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        cleaned = "\n".join(lines)

    try:
        return json.loads(cleaned)
    except json.JSONDecodeError as e:
        log.warning(f"[hermes] Не удалось распарсить JSON: {e} | Текст: {cleaned[:200]}")
        return None


# ══════════════════════════════════════════════════════
#  Публичные функции
# ══════════════════════════════════════════════════════

def should_trade(
    symbol: str,
    signal: str,
    price: float,
    rsi: float,
    adx: float,
    atr_pct: float,
    mode: str,
    vol_ratio: float,
    market_context: str = "",
    include_news: bool = True,
) -> tuple[bool, str]:
    """
    Спросить LLM: стоит ли открывать сделку?

    Аргументы:
        symbol:         тикер (DOGEUSDT, PEPEUSDT, ...)
        signal:         'BUY' или 'SELL'
        price:          текущая цена
        rsi:            значение RSI (14)
        adx:            значение ADX (14)
        atr_pct:        ATR как % от цены
        mode:           режим рынка ('trend' / 'sideways')
        vol_ratio:      отношение объёма к среднему (>1 = повышенный)
        market_context: дополнительный контекст (необязательно)
        include_news:   включить анализ новостей (по умолчанию True)

    Возвращает:
        (True/False, причина) — разрешение на сделку и объяснение.
        При ошибке/отключении: (True, "Hermes недоступен — торговля разрешена по умолчанию")
    """
    DEFAULT = (True, "Hermes недоступен — торговля разрешена по умолчанию")

    prompt = (
        f"Should this trade be taken?\n"
        f"Symbol: {symbol}\n"
        f"Signal: {signal}\n"
        f"Price: {price}\n"
        f"RSI(14): {rsi:.1f}\n"
        f"ADX(14): {adx:.1f}\n"
        f"ATR%: {atr_pct:.2f}%\n"
        f"Market mode: {mode}\n"
        f"Volume ratio: {vol_ratio:.2f}x\n"
    )

    # Анализ новостей (если включен)
    if include_news:
        news = search_news(symbol, lookback_hours=24)
        prompt += (
            f"\n--- Recent News Analysis ---\n"
            f"Sentiment: {news['sentiment']:+.2f} "
            f"({'bullish' if news['sentiment'] > 0 else 'bearish' if news['sentiment'] < 0 else 'neutral'})\n"
            f"Sources: {news['sources']}\n"
            f"Summary: {news['summary']}\n"
        )
        if news['red_flags']:
            prompt += f"⚠️ Red flags: {', '.join(news['red_flags'])}\n"

    if market_context:
        prompt += f"Additional context: {market_context}\n"

    prompt += (
        '\nRespond with JSON: {"trade": true/false, "confidence": 0.0-1.0, "reason": "brief explanation"}'
    )

    raw = _call_llm(prompt)
    parsed = _parse_json(raw)

    if parsed is None:
        return DEFAULT

    try:
        trade = bool(parsed.get("trade", True))
        confidence = float(parsed.get("confidence", 0.5))
        reason = str(parsed.get("reason", "без объяснения"))
        log.info(
            f"[hermes] should_trade({symbol} {signal}): "
            f"trade={trade}, confidence={confidence:.2f}, reason={reason}"
        )
        return (trade, f"[{confidence:.0%}] {reason}")
    except (ValueError, TypeError) as e:
        log.warning(f"[hermes] Ошибка разбора should_trade: {e}")
        return DEFAULT


def assess_market_sentiment(symbols: list[str]) -> dict[str, float]:
    """
    Оценка общего настроения рынка по списку монет.

    Аргументы:
        symbols: список тикеров ['DOGEUSDT', 'PEPEUSDT', ...]

    Возвращает:
        dict {symbol: sentiment_score} где score от -1.0 (медвежий) до 1.0 (бычий).
        При ошибке/отключении: {symbol: 0.0} для каждой монеты (нейтрально).
    """
    DEFAULT = {s: 0.0 for s in symbols}

    if not symbols:
        return {}

    prompt = (
        f"Assess the current market sentiment for these memecoin futures on Bybit.\n"
        f"Symbols: {', '.join(symbols)}\n"
        f"\nFor each symbol, provide a sentiment score from -1.0 (extreme bearish) "
        f"to 1.0 (extreme bullish). 0.0 is neutral.\n"
        f'\nRespond with JSON: {{"<SYMBOL>": {{"sentiment": -1.0 to 1.0, "summary": "brief"}}, ...}}'
    )

    raw = _call_llm(prompt)
    parsed = _parse_json(raw)

    if parsed is None:
        return DEFAULT

    result = {}
    try:
        for sym in symbols:
            if sym in parsed and isinstance(parsed[sym], dict):
                score = float(parsed[sym].get("sentiment", 0.0))
                # Ограничиваем диапазон
                score = max(-1.0, min(1.0, score))
                result[sym] = score
            else:
                result[sym] = 0.0

        log.info(f"[hermes] assess_market_sentiment: {result}")
        return result

    except (ValueError, TypeError) as e:
        log.warning(f"[hermes] Ошибка разбора sentiment: {e}")
        return DEFAULT


def search_news(symbol: str, lookback_hours: int = 24) -> dict:
    """
    Поиск новостей и Twitter-упоминаний о монете через web search.

    Аргументы:
        symbol:          тикер (DOGEUSDT, PEPEUSDT, ...)
        lookback_hours:  глубина поиска в часах (по умолчанию 24ч)

    Возвращает:
        dict {
            "sentiment": float (-1.0 до 1.0),
            "summary": str,
            "sources": int (количество найденных источников),
            "red_flags": list[str] (список тревожных сигналов)
        }
        При ошибке/отключении: нейтральные значения.
    """
    DEFAULT = {
        "sentiment": 0.0,
        "summary": "Новости недоступны — Hermes отключён",
        "sources": 0,
        "red_flags": [],
    }

    # Убираем "USDT" из тикера для более точного поиска
    coin_name = symbol.replace("USDT", "").replace("USD", "")

    prompt = (
        f"Search recent news and Twitter mentions about {coin_name} cryptocurrency "
        f"in the last {lookback_hours} hours. Focus on:\n"
        f"1. Price movements and market sentiment\n"
        f"2. Major announcements or partnerships\n"
        f"3. Concerns about scams, rug pulls, or red flags\n"
        f"4. Trading volume changes and whale activities\n"
        f"5. Community sentiment on Twitter/Reddit\n\n"
        f"Respond with JSON:\n"
        f'{{\n'
        f'  "sentiment": -1.0 to 1.0 (bearish to bullish),\n'
        f'  "summary": "brief 2-3 sentence summary in English",\n'
        f'  "sources": number of relevant sources found,\n'
        f'  "red_flags": ["list", "of", "concerns"] or empty array\n'
        f'}}'
    )

    raw = _call_llm(prompt)
    parsed = _parse_json(raw)

    if parsed is None:
        return DEFAULT

    try:
        result = {
            "sentiment": float(parsed.get("sentiment", 0.0)),
            "summary": str(parsed.get("summary", "нет данных")),
            "sources": int(parsed.get("sources", 0)),
            "red_flags": parsed.get("red_flags", [])
            if isinstance(parsed.get("red_flags"), list)
            else [],
        }
        # Ограничиваем sentiment диапазоном
        result["sentiment"] = max(-1.0, min(1.0, result["sentiment"]))

        log.info(
            f"[hermes] search_news({symbol}): sentiment={result['sentiment']:.2f}, "
            f"sources={result['sources']}, flags={len(result['red_flags'])}"
        )
        return result

    except (ValueError, TypeError) as e:
        log.warning(f"[hermes] Ошибка разбора search_news: {e}")
        return DEFAULT


def check_rug_pull_risk(
    symbol: str,
    price_change_24h: float,
    volume_24h: float,
) -> tuple[str, str]:
    """
    Проверка риска rug-pull / резкого обвала для мемкоина.

    Аргументы:
        symbol:            тикер
        price_change_24h:  изменение цены за 24ч (%)
        volume_24h:        объём торгов за 24ч (USDT)

    Возвращает:
        (risk_level, reason) где risk_level: 'low' / 'medium' / 'high'.
        При ошибке/отключении: ('low', "Hermes недоступен — риск не оценён")
    """
    DEFAULT = ("low", "Hermes недоступен — риск не оценён")

    prompt = (
        f"Assess rug-pull / sudden crash risk for this memecoin on Bybit futures.\n"
        f"Symbol: {symbol}\n"
        f"Price change 24h: {price_change_24h:+.2f}%\n"
        f"Volume 24h: ${volume_24h:,.0f}\n"
        f"\nConsider:\n"
        f"- Extreme price pumps (>50% in 24h) often precede dumps\n"
        f"- Very low volume means thin liquidity → higher slippage risk\n"
        f"- Memecoins are inherently volatile\n"
        f'\nRespond with JSON: {{"risk": "low"/"medium"/"high", "reason": "explanation"}}'
    )

    raw = _call_llm(prompt)
    parsed = _parse_json(raw)

    if parsed is None:
        return DEFAULT

    try:
        risk = str(parsed.get("risk", "low")).lower()
        if risk not in ("low", "medium", "high"):
            risk = "low"
        reason = str(parsed.get("reason", "без объяснения"))
        log.info(f"[hermes] check_rug_pull_risk({symbol}): risk={risk}, reason={reason}")
        return (risk, reason)

    except (ValueError, TypeError) as e:
        log.warning(f"[hermes] Ошибка разбора rug_pull_risk: {e}")
        return DEFAULT


def explain_decision(
    symbol: str,
    action: str,
    context: dict,
) -> str:
    """
    Сгенерировать человекочитаемое объяснение для Telegram.

    Аргументы:
        symbol:  тикер
        action:  'OPEN_LONG' / 'OPEN_SHORT' / 'CLOSE' / 'SKIP' и т.д.
        context: словарь с данными (rsi, adx, price, pnl, reason, ...)

    Возвращает:
        Строка с объяснением на русском.
        При ошибке/отключении: краткое стандартное объяснение.
    """
    DEFAULT = f"{action} {symbol} — автоматическое решение бота"

    system = (
        "You are a memecoin trading advisor. Generate a brief, clear explanation "
        "in Russian (1-3 sentences) for a Telegram notification about a trading "
        "decision. Use emoji for readability. Do NOT wrap in JSON — return plain text only."
    )

    context_str = "\n".join(f"  {k}: {v}" for k, v in context.items())
    prompt = (
        f"Explain this trading decision:\n"
        f"Symbol: {symbol}\n"
        f"Action: {action}\n"
        f"Context:\n{context_str}\n"
        f"\nWrite a brief explanation in Russian with emoji. Plain text, no JSON."
    )

    raw = _call_llm(prompt, system_prompt=system)

    if raw is None:
        return DEFAULT

    # Ответ — просто текст, не JSON
    explanation = raw.strip()
    if not explanation:
        return DEFAULT

    log.info(f"[hermes] explain_decision({symbol} {action}): {explanation[:100]}")
    return explanation


def daily_summary(
    trades_today: int,
    total_pnl: float,
    by_symbol: dict,
) -> str:
    """
    Сгенерировать ежедневную сводку для Telegram.

    Аргументы:
        trades_today: количество сделок за сегодня
        total_pnl:    общий PnL за день (USDT)
        by_symbol:    {symbol: {"pnl": float, "count": int, "wins": int}} — PnL по монетам

    Возвращает:
        Строка с анализом дня на русском.
        При ошибке/отключении: стандартная сводка.
    """
    DEFAULT = (
        f"📊 Итоги дня: {trades_today} сделок, PnL: {total_pnl:+.2f} USDT\n"
        f"{'✅ В плюсе' if total_pnl >= 0 else '❌ В минусе'}"
    )

    system = (
        "You are a memecoin trading advisor. Generate an end-of-day analysis "
        "in Russian (3-5 sentences) for a Telegram message. Use emoji. "
        "Include actionable insights based on the data. Do NOT wrap in JSON — "
        "return plain text only."
    )

    # Форматируем by_symbol для промпта
    sym_lines = []
    for sym, data in by_symbol.items():
        pnl = data.get("pnl", 0)
        count = data.get("count", 0)
        wins = data.get("wins", 0)
        wr = (wins / count * 100) if count > 0 else 0
        sym_lines.append(f"  {sym}: PnL={pnl:+.2f}$, сделок={count}, WR={wr:.0f}%")

    prompt = (
        f"Generate an end-of-day trading summary:\n"
        f"Total trades: {trades_today}\n"
        f"Total PnL: {total_pnl:+.2f} USDT\n"
        f"\nBy symbol:\n" + "\n".join(sym_lines) +
        f"\n\nProvide analysis in Russian with emoji. Mention what worked, "
        f"what didn't, and what to adjust. Plain text, no JSON."
    )

    raw = _call_llm(prompt, system_prompt=system)

    if raw is None:
        return DEFAULT

    summary = raw.strip()
    if not summary:
        return DEFAULT

    log.info(f"[hermes] daily_summary: {summary[:100]}")
    return summary


# ══════════════════════════════════════════════════════
#  Утилиты для отладки / ручного вызова
# ══════════════════════════════════════════════════════

def health_check() -> bool:
    """
    Проверить доступность Hermes API.

    Возвращает:
        True если Hermes отвечает, False если нет.
    """
    if not _is_enabled():
        return False

    try:
        # Пробуем обратиться к базовому URL (без /chat/completions)
        base_url = HERMES_API_URL.rsplit("/chat/completions", 1)[0]
        resp = requests.get(f"{base_url}/models", timeout=5)
        ok = resp.status_code == 200
        log.info(f"[hermes] health_check: {'OK' if ok else 'FAIL'} (status={resp.status_code})")
        return ok
    except Exception as e:
        log.warning(f"[hermes] health_check: FAIL ({e})")
        return False


def get_status() -> dict:
    """
    Получить статус модуля для отображения в Telegram.

    Возвращает:
        dict с информацией о состоянии модуля.
    """
    global _last_call_ts
    now = time.time()
    since_last = now - _last_call_ts if _last_call_ts > 0 else -1

    return {
        "enabled": HERMES_ENABLED,
        "api_url": HERMES_API_URL,
        "model": HERMES_MODEL,
        "timeout": HERMES_TIMEOUT,
        "rate_limit_sec": RATE_LIMIT_SEC,
        "seconds_since_last_call": round(since_last, 1) if since_last >= 0 else "нет вызовов",
        "rate_limited": since_last >= 0 and since_last < RATE_LIMIT_SEC,
    }


# ── Для запуска из командной строки (тест) ────────────

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.DEBUG,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )

    print("=" * 50)
    print("Hermes Brain — тест модуля")
    print("=" * 50)

    status = get_status()
    print(f"\nСтатус: {json.dumps(status, indent=2, ensure_ascii=False, default=str)}")

    if not _is_enabled():
        print("\n⚠️  Hermes отключён (HERMES_ENABLED != 1)")
        print("Тест с дефолтными значениями:\n")

    # Тест should_trade
    ok, reason = should_trade(
        symbol="DOGEUSDT",
        signal="BUY",
        price=0.0823,
        rsi=28.5,
        adx=32.0,
        atr_pct=1.85,
        mode="trend",
        vol_ratio=1.45,
    )
    print(f"should_trade: trade={ok}, reason={reason}")

    # Тест assess_market_sentiment
    sentiment = assess_market_sentiment(["DOGEUSDT", "PEPEUSDT", "SHIBUSDT"])
    print(f"assess_market_sentiment: {sentiment}")

    # Тест check_rug_pull_risk
    risk, risk_reason = check_rug_pull_risk("PEPEUSDT", 85.3, 12_500_000)
    print(f"check_rug_pull_risk: risk={risk}, reason={risk_reason}")

    # Тест explain_decision
    explanation = explain_decision("DOGEUSDT", "OPEN_LONG", {
        "rsi": 28.5, "adx": 32.0, "price": 0.0823, "mode": "trend",
    })
    print(f"explain_decision: {explanation}")

    # Тест daily_summary
    summary = daily_summary(
        trades_today=12,
        total_pnl=3.45,
        by_symbol={
            "DOGEUSDT": {"pnl": 5.2, "count": 5, "wins": 4},
            "PEPEUSDT": {"pnl": -1.75, "count": 7, "wins": 2},
        },
    )
    print(f"daily_summary: {summary}")

    print("\n✅ Тест завершён")
