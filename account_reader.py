"""
╔══════════════════════════════════════════════╗
║   ACCOUNT_READER.PY — Сводка по счёту Bybit  ║
║   Только чтение: баланс UTA + открытые       ║
║   USDT-позиции. Ордеров НЕ ставит.           ║
╚══════════════════════════════════════════════╝

Использует ОТДЕЛЬНЫЙ read-only ключ Bybit:
  BYBIT_RO_API_KEY / BYBIT_RO_API_SECRET  (в .env)
Торговый ключ BYBIT_API_KEY здесь намеренно не используется.
Перед каждым запросом проверяет на стороне Bybit, что ключ
действительно read-only (GET /v5/user/query-api -> readOnly == 1).

Перенесено из плагина Artemtru/vibe-trading-bybit-readonly.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import time
from datetime import datetime, timedelta, timezone

import requests
from dotenv import load_dotenv

load_dotenv()

BASE_URL    = os.getenv("BYBIT_RO_BASE_URL", "https://api.bybit.com").rstrip("/")
RECV_WINDOW = "5000"
TIMEOUT_S   = 15
MSK         = timezone(timedelta(hours=3))


def _keys():
    return os.getenv("BYBIT_RO_API_KEY", ""), os.getenv("BYBIT_RO_API_SECRET", "")


def _signed_get(path: str, params: dict) -> dict:
    api_key, api_secret = _keys()
    if not api_key or not api_secret:
        raise RuntimeError("не заданы BYBIT_RO_API_KEY / BYBIT_RO_API_SECRET в .env")
    clean = {k: v for k, v in params.items() if v not in (None, "")}
    query = "&".join(f"{k}={v}" for k, v in clean.items())
    ts = str(int(time.time() * 1000))
    sign = hmac.new(
        api_secret.encode(), f"{ts}{api_key}{RECV_WINDOW}{query}".encode(), hashlib.sha256
    ).hexdigest()
    headers = {
        "X-BAPI-API-KEY": api_key,
        "X-BAPI-TIMESTAMP": ts,
        "X-BAPI-RECV-WINDOW": RECV_WINDOW,
        "X-BAPI-SIGN": sign,
        "X-BAPI-SIGN-TYPE": "2",
    }
    url = f"{BASE_URL}{path}" + (f"?{query}" if query else "")
    try:
        resp = requests.get(url, headers=headers, timeout=TIMEOUT_S)
    except requests.RequestException as e:
        raise RuntimeError(f"Bybit недоступен ({path}): {e.__class__.__name__}") from None
    if resp.status_code != 200:
        raise RuntimeError(f"Bybit HTTP {resp.status_code} ({path})")
    body = resp.json()
    if body.get("retCode") != 0:
        raise RuntimeError(f"Bybit {path}: retCode={body.get('retCode')} {body.get('retMsg')}")
    return body.get("result") or {}


def _num(v):
    try:
        return float(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _require_readonly_key():
    info = _signed_get("/v5/user/query-api", {})
    if str(info.get("readOnly")) != "1":
        raise RuntimeError("ключ BYBIT_RO_* имеет права на торговлю — создай ключ Read-Only")


def get_balance() -> dict:
    result = _signed_get("/v5/account/wallet-balance", {"accountType": "UNIFIED"})
    rows = result.get("list") or []
    s = rows[0] if rows else {}
    coins = [
        {
            "coin": c.get("coin"),
            "wallet": _num(c.get("walletBalance")),
            "usd": _num(c.get("usdValue")),
        }
        for c in s.get("coin") or []
        if _num(c.get("walletBalance"))
    ]
    usdt = next((c["wallet"] for c in coins if c["coin"] == "USDT"), 0.0)
    return {
        "total_equity": _num(s.get("totalEquity")),
        "available": _num(s.get("totalAvailableBalance")),
        "upl": _num(s.get("totalPerpUPL")),
        "usdt": usdt,
        "coins": coins,
    }


def get_positions() -> list:
    out, cursor = [], ""
    for _ in range(20):
        r = _signed_get(
            "/v5/position/list",
            {"category": "linear", "settleCoin": "USDT", "limit": 200, "cursor": cursor},
        )
        for p in r.get("list") or []:
            size = _num(p.get("size"))
            if not size:
                continue
            out.append({
                "symbol": p.get("symbol"),
                "side": p.get("side"),
                "size": size,
                "avg": _num(p.get("avgPrice")),
                "mark": _num(p.get("markPrice")),
                "value": _num(p.get("positionValue")),
                "upl": _num(p.get("unrealisedPnl")) or 0.0,
                "lev": p.get("leverage"),
                "liq": _num(p.get("liqPrice")),
            })
        cursor = r.get("nextPageCursor") or ""
        if not cursor:
            break
    return out


def snapshot() -> dict:
    _require_readonly_key()
    return {"balance": get_balance(), "positions": get_positions()}


def _fmt(v, digits=2):
    return "—" if v is None else f"{v:,.{digits}f}".replace(",", " ")


def format_snapshot(title: str = "💼 Счёт Bybit") -> str:
    """HTML-текст для Telegram. Ошибки возвращаются текстом, не исключением."""
    try:
        data = snapshot()
    except Exception as e:
        return f"⚠️ {title}: не удалось получить данные.\n{e}"

    b, pos = data["balance"], data["positions"]
    upl = b["upl"] or 0.0
    lines = [
        f"<b>{title}</b>",
        f"<i>{datetime.now(MSK):%d.%m.%Y %H:%M} МСК</i>\n",
        f"Капитал (equity): <b>{_fmt(b['total_equity'])} $</b>",
        f"USDT на счёте: {_fmt(b['usdt'])}",
        f"Доступно: {_fmt(b['available'])} $",
        f"Нереализ. PnL: {upl:+.2f} $",
    ]
    others = [c for c in b["coins"] if c["coin"] != "USDT"]
    if others:
        lines.append("Другие монеты: " + ", ".join(
            f"{c['coin']} {c['wallet']:g} (~{_fmt(c['usd'])} $)" for c in others))

    lines.append(f"\n📂 <b>Открытые позиции: {len(pos)}</b>")
    if not pos:
        lines.append("нет")
    for p in pos:
        arrow = "🟢 LONG" if p["side"] == "Buy" else "🔴 SHORT"
        lines.append(
            f"<b>{p['symbol']}</b> {arrow} x{p['lev']}\n"
            f"  Объём: {p['size']:g} (~{_fmt(p['value'])} $)\n"
            f"  Вход: {p['avg']:g} → сейчас {p['mark']:g}\n"
            f"  PnL: {p['upl']:+.2f} $ | Ликв.: " + (f"{p['liq']:g}" if p['liq'] else "—")
        )
    return "\n".join(lines)


# ---------- Утренняя сводка ------------------------------------------------

MORNING_TIME = os.getenv("MORNING_REPORT_TIME", "09:00")      # МСК, HH:MM; пусто = выкл
MARKER_FILE  = "logs/morning_report_last.txt"


def morning_report_due(now: datetime | None = None) -> bool:
    """True, если пора отправить сводку и сегодня её ещё не было."""
    if not MORNING_TIME:
        return False
    now = now or datetime.now(MSK)
    try:
        hh, mm = (int(x) for x in MORNING_TIME.split(":"))
    except ValueError:
        return False
    if (now.hour, now.minute) < (hh, mm):
        return False
    today = now.strftime("%Y-%m-%d")
    try:
        with open(MARKER_FILE, encoding="utf-8") as f:
            if f.read().strip() == today:
                return False
    except FileNotFoundError:
        pass
    return True


def mark_morning_report_sent(now: datetime | None = None) -> None:
    now = now or datetime.now(MSK)
    os.makedirs(os.path.dirname(MARKER_FILE), exist_ok=True)
    with open(MARKER_FILE, "w", encoding="utf-8") as f:
        f.write(now.strftime("%Y-%m-%d"))


if __name__ == "__main__":
    print(format_snapshot())
