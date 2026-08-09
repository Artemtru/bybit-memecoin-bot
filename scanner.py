"""
╔══════════════════════════════════════════════╗
║         SCANNER.PY — Поиск монет            ║
║  Сканирует Bybit фьючерсы, находит          ║
║  волатильные мемкоины по критериям          ║
╚══════════════════════════════════════════════╝
"""

import os
from dotenv import load_dotenv
from pybit.unified_trading import HTTP

load_dotenv()

session = HTTP(
    testnet=False,
    api_key=os.getenv("BYBIT_API_KEY"),
    api_secret=os.getenv("BYBIT_API_SECRET"),
)

# ── Известные мемкоины на Bybit фьючерсах ───────
MEME_KEYWORDS = [
    "POPCAT", "BONK", "WIF", "PEPE", "DOGE", "SHIB",
    "FLOKI", "MEME", "NEIRO", "COW", "GOAT", "PNUT",
    "ACT", "TURBO", "BRETT", "MOG", "BOME", "PONKE",
    "SLERF", "BOOK", "MEW", "GIGA", "MOODENG", "HMSTR",
    "CATI", "DOGS", "MAJOR", "NOT", "LADYS", "COQ",
    "MYRO", "PURR", "SELFIE", "FWOG", "SUNDOG",
]

def get_all_tickers():
    """Получить все фьючерсные тикеры с Bybit"""
    resp = session.get_tickers(category="linear")
    return resp["result"]["list"]


def calc_rsi_from_klines(symbol, interval="15", period=14):
    """Вычислить RSI для символа"""
    try:
        resp = session.get_kline(
            category="linear",
            symbol=symbol,
            interval=interval,
            limit=period + 10,
        )
        candles = resp["result"]["list"]
        closes = [float(c[4]) for c in reversed(candles)]

        gains, losses = [], []
        for i in range(1, len(closes)):
            diff = closes[i] - closes[i - 1]
            gains.append(max(diff, 0))
            losses.append(max(-diff, 0))

        avg_gain = sum(gains[:period]) / period
        avg_loss = sum(losses[:period]) / period

        for i in range(period, len(gains)):
            avg_gain = (avg_gain * (period - 1) + gains[i]) / period
            avg_loss = (avg_loss * (period - 1) + losses[i]) / period

        if avg_loss == 0:
            return 100.0
        rs = avg_gain / avg_loss
        return round(100 - (100 / (1 + rs)), 2)
    except Exception:
        return None


def scan(
    min_volume_usdt=250_000,     # минимальный объём 24h в USDT (смягчено: 500K→250K)
    min_change_pct=3.0,          # минимальное изменение цены за 24h (%)
    max_change_pct=30.0,         # максимум — защита от пампов (смягчено: 15%→30%)
    rsi_min=20,                  # RSI не ниже (смягчено: 25→20)
    rsi_max=80,                  # RSI не выше (смягчено: 75→80)
    top_n=5,                     # сколько монет вернуть
):
    """
    Сканирует все фьючерсы Bybit, фильтрует мемкоины
    по объёму, волатильности и RSI.
    Возвращает список словарей с данными монет.
    """
    print("🔍 Сканирую рынок Bybit...")
    tickers = get_all_tickers()

    candidates = []
    for t in tickers:
        symbol = t.get("symbol", "")

        # Только USDT-перп и только мемкоины
        if not symbol.endswith("USDT"):
            continue
        base = symbol.replace("USDT", "")
        if not any(kw in base for kw in MEME_KEYWORDS):
            continue

        try:
            volume_24h  = float(t.get("turnover24h", 0))   # объём в USDT
            change_pct  = abs(float(t.get("price24hPcnt", 0)) * 100)
            last_price  = float(t.get("lastPrice", 0))
        except (ValueError, TypeError):
            continue

        # Фильтр по объёму и волатильности
        if volume_24h < min_volume_usdt:
            continue
        if change_pct < min_change_pct or change_pct > max_change_pct:
            continue
        if last_price <= 0:
            continue

        candidates.append({
            "symbol":     symbol,
            "price":      last_price,
            "change_pct": round(change_pct, 2),
            "volume_24h": volume_24h,
        })

    print(f"   Найдено кандидатов: {len(candidates)}")

    # Сортируем по объёму (самые ликвидные — лучше)
    candidates.sort(key=lambda x: x["volume_24h"], reverse=True)

    # Получаем RSI для топ-кандидатов и финально фильтруем
    result = []
    for coin in candidates[:top_n * 3]:   # берём с запасом
        rsi = calc_rsi_from_klines(coin["symbol"])
        coin["rsi"] = rsi

        if rsi is None:
            continue
        if not (rsi_min <= rsi <= rsi_max):
            continue

        result.append(coin)
        if len(result) >= top_n:
            break

    return result


def print_scan_results(coins):
    """Красивый вывод результатов сканирования"""
    print("\n" + "=" * 58)
    print("  🐸 ТОП МЕМКОИНЫ ДЛЯ ТОРГОВЛИ")
    print("=" * 58)
    if not coins:
        print("  Подходящих монет не найдено — рынок спокойный.")
    for i, c in enumerate(coins, 1):
        vol_m = c["volume_24h"] / 1_000_000
        print(
            f"  {i}. {c['symbol']:<16} "
            f"${c['price']:.6f}  "
            f"±{c['change_pct']}%  "
            f"Vol:{vol_m:.1f}M  "
            f"RSI:{c['rsi']}"
        )
    print("=" * 58)


if __name__ == "__main__":
    coins = scan()
    print_scan_results(coins)
