"""
╔══════════════════════════════════════════════════════╗
║        HERMES_BRAIN.PY v2 — LLM-интеграция          ║
║                                                      ║
║  Fixes v2:                                           ║
║  • Added Authorization header (HERMES_API_KEY)      ║
║  • load_dotenv() at top — HERMES_ENABLED correct    ║
║  • call_llm / parse_json now public                 ║
║  • _call_llm / _parse_json kept as aliases          ║
║  • search_news() returns stub — no internet         ║
║  • should_trade: include_news=False default         ║
║  • health_check hits /v1/models not /health         ║
╚══════════════════════════════════════════════════════╝
"""

import os
import json
import time
import logging
import requests
from dotenv import load_dotenv

# FIX: load_dotenv at top so HERMES_ENABLED is read correctly
load_dotenv()

log = logging.getLogger("hermes_brain")

HERMES_API_URL = os.getenv("HERMES_API_URL", "http://localhost:8642/v1/chat/completions")
HERMES_API_KEY = os.getenv("HERMES_API_KEY", "")   # FIX: used in Authorization header
HERMES_ENABLED = os.getenv("HERMES_ENABLED", "0") == "1"
HERMES_MODEL   = os.getenv("HERMES_MODEL",   "hermes-agent")
HERMES_TIMEOUT = int(os.getenv("HERMES_TIMEOUT", "30"))

RATE_LIMIT_SEC = 30
_last_call_ts  = 0.0

SYSTEM_PROMPT = (
    "You are a memecoin trading advisor for an automated bot on Bybit futures. "
    "The bot uses RSI + Bollinger Bands + ATR on 5-min candles. "
    "Your role is to provide a final safety check before trades. "
    "Be concise — respond with JSON only."
)


def _is_enabled() -> bool:
    return HERMES_ENABLED


def _rate_limited() -> bool:
    global _last_call_ts
    if time.time() - _last_call_ts < RATE_LIMIT_SEC:
        log.debug(f"[hermes] Rate limited")
        return True
    return False


def call_llm(user_prompt: str, system_prompt: str = SYSTEM_PROMPT) -> str | None:
    """Public LLM call with Authorization header."""
    global _last_call_ts
    if not _is_enabled():
        log.debug("[hermes] disabled")
        return None
    if _rate_limited():
        return None

    payload = {
        "model": HERMES_MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user",   "content": user_prompt},
        ],
        "temperature": 0.3,
        "max_tokens":  512,
    }
    # FIX: Authorization header was missing in original
    headers = {"Content-Type": "application/json"}
    if HERMES_API_KEY:
        headers["Authorization"] = f"Bearer {HERMES_API_KEY}"

    log.info(f"[hermes] → {user_prompt[:150]}...")
    try:
        _last_call_ts = time.time()
        resp = requests.post(HERMES_API_URL, json=payload, headers=headers, timeout=HERMES_TIMEOUT)
        resp.raise_for_status()
        content = resp.json()["choices"][0]["message"]["content"]
        log.info(f"[hermes] ← {content[:200]}")
        return content
    except requests.exceptions.Timeout:
        log.warning(f"[hermes] Timeout ({HERMES_TIMEOUT}s)")
    except requests.exceptions.ConnectionError:
        log.warning("[hermes] ConnectionError")
    except requests.exceptions.HTTPError as e:
        log.warning(f"[hermes] HTTP error: {e}")
    except (KeyError, IndexError, json.JSONDecodeError) as e:
        log.warning(f"[hermes] Bad response: {e}")
    except Exception as e:
        log.error(f"[hermes] Unexpected: {e}", exc_info=True)
    return None


# FIX: keep private aliases for backward compatibility with ai_strategy_manager
_call_llm = call_llm


def parse_json(text: str | None) -> dict | None:
    """Public JSON parser for LLM responses."""
    if text is None:
        return None
    cleaned = text.strip()
    if cleaned.startswith("```"):
        lines = cleaned.split("\n")
        if lines[0].startswith("```"): lines = lines[1:]
        if lines and lines[-1].strip() == "```": lines = lines[:-1]
        cleaned = "\n".join(lines)
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError as e:
        log.warning(f"[hermes] JSON parse error: {e} | {cleaned[:100]}")
        return None


# FIX: keep private alias
_parse_json = parse_json


def should_trade(
    symbol: str,
    signal: str,      # FIX: "BUY" or "SELL" (not side="long"/"short")
    price:  float,
    rsi:    float,
    adx:    float,
    atr_pct: float,
    mode:   str,
    vol_ratio: float,
    market_context: str = "",
    include_news: bool = False,  # FIX: False by default — no internet access via OpenRouter
) -> tuple[bool, str]:
    DEFAULT = (True, "Hermes unavailable — trade allowed by default")

    prompt = (
        f"Should this trade be taken?\n"
        f"Symbol: {symbol}\nSignal: {signal}\nPrice: {price}\n"
        f"RSI(14): {rsi:.1f}\nADX(14): {adx:.1f}\nATR%: {atr_pct:.2f}%\n"
        f"Market mode: {mode}\nVolume ratio: {vol_ratio:.2f}x\n"
    )
    if market_context:
        prompt += f"Context: {market_context}\n"
    prompt += '\nRespond: {"trade": true/false, "confidence": 0.0-1.0, "reason": "..."}'

    raw    = call_llm(prompt)
    parsed = parse_json(raw)
    if parsed is None:
        return DEFAULT
    try:
        trade      = bool(parsed.get("trade", True))
        confidence = float(parsed.get("confidence", 0.5))
        reason     = str(parsed.get("reason", "no reason"))
        log.info(f"[hermes] should_trade({symbol} {signal}): {trade} [{confidence:.0%}] {reason}")
        return (trade, f"[{confidence:.0%}] {reason}")
    except Exception:
        return DEFAULT


def assess_market_sentiment(symbols: list[str]) -> dict[str, float]:
    DEFAULT = {s: 0.0 for s in symbols}
    if not symbols:
        return {}
    prompt = (
        f"Assess sentiment for memecoins: {', '.join(symbols)}\n"
        f'Respond: {{"<SYMBOL>": {{"sentiment": -1.0 to 1.0}}, ...}}'
    )
    raw    = call_llm(prompt)
    parsed = parse_json(raw)
    if parsed is None:
        return DEFAULT
    result = {}
    for sym in symbols:
        if sym in parsed and isinstance(parsed[sym], dict):
            result[sym] = max(-1.0, min(1.0, float(parsed[sym].get("sentiment", 0.0))))
        else:
            result[sym] = 0.0
    return result


def search_news(symbol: str, lookback_hours: int = 24) -> dict:
    """
    FIX: Hermes has no internet access via OpenRouter — returns neutral stub.
    Calling LLM for news would produce hallucinations.
    """
    return {
        "sentiment": 0.0,
        "summary":   "News search disabled (LLM has no internet access)",
        "sources":   0,
        "red_flags": [],
    }


def check_rug_pull_risk(symbol: str, price_change_24h: float, volume_24h: float) -> tuple[str, str]:
    DEFAULT = ("low", "Hermes unavailable")
    prompt  = (
        f"Rug-pull risk for {symbol} futures.\n"
        f"Price 24h: {price_change_24h:+.2f}%  Volume 24h: ${volume_24h:,.0f}\n"
        f'Respond: {{"risk": "low"/"medium"/"high", "reason": "..."}}'
    )
    raw    = call_llm(prompt)
    parsed = parse_json(raw)
    if parsed is None:
        return DEFAULT
    risk = str(parsed.get("risk", "low")).lower()
    if risk not in ("low", "medium", "high"):
        risk = "low"
    return (risk, str(parsed.get("reason", "no reason")))


def explain_decision(symbol: str, action: str, context: dict) -> str:
    DEFAULT = f"{action} {symbol} — automatic bot decision"
    system  = (
        "You are a trading advisor. Write a brief explanation in Russian "
        "(1-3 sentences) with emoji. Plain text, no JSON."
    )
    ctx_str = "\n".join(f"  {k}: {v}" for k, v in context.items())
    raw     = call_llm(f"Explain: {action} {symbol}\n{ctx_str}", system_prompt=system)
    return raw.strip() if raw else DEFAULT


def daily_summary(trades_today: int, total_pnl: float, by_symbol: dict) -> str:
    DEFAULT = (
        f"📊 Итоги: {trades_today} сделок, PnL: {total_pnl:+.2f} USDT\n"
        f"{'✅ В плюсе' if total_pnl >= 0 else '❌ В минусе'}"
    )
    system = (
        "Generate end-of-day trading summary in Russian (3-5 sentences) "
        "with emoji. Plain text, no JSON."
    )
    sym_lines = [
        f"  {s}: PnL={d.get('pnl',0):+.2f}$, count={d.get('count',0)}, "
        f"WR={d.get('wins',0)/max(d.get('count',1),1)*100:.0f}%"
        for s, d in by_symbol.items()
    ]
    prompt = (
        f"Summary: {trades_today} trades, PnL={total_pnl:+.2f} USDT\n"
        + "\n".join(sym_lines)
    )
    raw = call_llm(prompt, system_prompt=system)
    return raw.strip() if raw else DEFAULT


def health_check() -> bool:
    if not _is_enabled():
        return False
    try:
        base    = HERMES_API_URL.rsplit("/chat/completions", 1)[0]
        headers = {}
        if HERMES_API_KEY:
            headers["Authorization"] = f"Bearer {HERMES_API_KEY}"
        # FIX: check /v1/models — Hermes does not have /health endpoint
        resp = requests.get(f"{base}/models", headers=headers, timeout=5)
        ok   = resp.status_code == 200
        log.info(f"[hermes] health: {'OK' if ok else 'FAIL'} ({resp.status_code})")
        return ok
    except Exception as e:
        log.warning(f"[hermes] health FAIL: {e}")
        return False


def get_status() -> dict:
    global _last_call_ts
    since = time.time() - _last_call_ts if _last_call_ts > 0 else -1
    return {
        "enabled":                  HERMES_ENABLED,
        "api_url":                  HERMES_API_URL,
        "model":                    HERMES_MODEL,
        "timeout":                  HERMES_TIMEOUT,
        "rate_limit_sec":           RATE_LIMIT_SEC,
        "seconds_since_last_call":  round(since, 1) if since >= 0 else "no calls yet",
        "rate_limited":             since >= 0 and since < RATE_LIMIT_SEC,
    }


if __name__ == "__main__":
    import logging as _l
    _l.basicConfig(level=_l.DEBUG, format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")
    print("Hermes Brain v2 — status:")
    print(json.dumps(get_status(), indent=2, ensure_ascii=False, default=str))
    print("health_check:", health_check())
    ok, reason = should_trade("DOGEUSDT", "BUY", 0.082, 28.5, 32.0, 1.85, "trend", 1.45)
    print(f"should_trade: {ok}, {reason}")
