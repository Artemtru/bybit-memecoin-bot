#!/usr/bin/env python3
"""
╔══════════════════════════════════════════════════════════════╗
║  analyze_bot_performance.py — УМНЫЙ ОПТИМИЗАТОР (v2.0)       ║
║                                                              ║
║  Читает РЕАЛЬНЫЕ сделки через analytics.py (trades.json)    ║
║  и осторожно подстраивает ТОЛЬКО безопасные параметры.      ║
║                                                              ║
║  Принципы:                                                   ║
║   • НЕ трогает структурные параметры (BB_PERIOD, ATR_PERIOD, ║
║     SCAN_INTERVAL_MIN, RSI_PERIOD) — их меняет только человек║
║   • LEVERAGE всегда целое, в диапазоне [1, 3]               ║
║   • DAILY_LOSS_LIMIT никогда не поднимается выше -50         ║
║   • Дополняет, а не заменяет живой strategy_adapter.py       ║
║   • Меняет параметры маленькими шагами, только при ≥N сделок ║
╚══════════════════════════════════════════════════════════════╝

Запуск (cron): 0 4 * * * cd /root/bybit-memecoin-bot && \
    venv/bin/python scripts/analyze_bot_performance.py >> reports/cron.log 2>&1
"""

import os
import sys
from datetime import datetime
from pathlib import Path

# ── Путь к боту ──────────────────────────────────────
# Скрипт должен работать из директории бота, чтобы analytics.py
# и pnl_tracker.py нашли logs/trades.json (относительный путь).
BOT_DIR = Path(os.getenv("BOT_DIR", "/root/bybit-memecoin-bot"))
ENV_FILE = BOT_DIR / ".env"
REPORTS_DIR = BOT_DIR / "reports"

# Переходим в директорию бота и добавляем в sys.path
os.chdir(BOT_DIR)
sys.path.insert(0, str(BOT_DIR))

try:
    import analytics
except ImportError as e:
    print(f"❌ Не удалось импортировать analytics.py из {BOT_DIR}: {e}")
    sys.exit(1)


# ── ПРЕДЕЛЫ БЕЗОПАСНОСТИ (жёсткие границы) ───────────
LIMITS = {
    "LEVERAGE":         {"min": 1,    "max": 3,    "type": int},
    "QTY_USDT":         {"min": 5.0,  "max": 30.0, "type": float},
    "ATR_TP_MULT":      {"min": 1.5,  "max": 3.5,  "type": float},
    "ATR_SL_MULT":      {"min": 0.5,  "max": 1.5,  "type": float},
    "BB_STD":           {"min": 1.8,  "max": 3.0,  "type": float},
    "RSI_BUY":          {"min": 30.0, "max": 48.0, "type": float},
    "RSI_SELL":         {"min": 52.0, "max": 70.0, "type": float},
    "DAILY_LOSS_LIMIT": {"min": -100.0, "max": -50.0, "type": float},  # никогда не мягче -50
}

# СТРУКТУРНЫЕ параметры — НИКОГДА не трогаем автоматически
FROZEN = {"BB_PERIOD", "ATR_PERIOD", "SCAN_INTERVAL_MIN", "RSI_PERIOD",
          "INTERVAL", "MAX_BOTS", "SIDEWAYS_ATR_THRESHOLD"}

# Минимум сделок, чтобы вообще что-то менять
MIN_TRADES = 10

# Шаги изменений
STEP = {
    "ATR_TP_MULT": 0.2,
    "ATR_SL_MULT": 0.1,
    "BB_STD":      0.2,
    "RSI":         2.0,
    "QTY_USDT":    2.0,
}


def read_env() -> dict:
    """Прочитать .env в словарь (сохраняя порядок и комментарии отдельно)."""
    values = {}
    if not ENV_FILE.exists():
        print(f"⚠️  .env не найден: {ENV_FILE}")
        return values
    with open(ENV_FILE, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, val = line.split("=", 1)
            values[key.strip()] = val.strip()
    return values


def clamp(key: str, value) -> "int|float":
    """Ограничить значение пределами безопасности и привести к типу."""
    lim = LIMITS[key]
    value = lim["type"](float(value))
    value = max(lim["min"], min(lim["max"], value))
    if lim["type"] is int:
        return int(round(value))
    return round(value, 2)


def get_num(env: dict, key: str, default):
    """Безопасно достать число из env с дефолтом."""
    try:
        return float(env.get(key, default))
    except (ValueError, TypeError):
        return float(default)


def write_env(updates: dict):
    """Записать обновления в .env, сохраняя все прочие строки без изменений."""
    if not ENV_FILE.exists():
        print(f"⚠️  .env не найден, запись отменена: {ENV_FILE}")
        return False

    with open(ENV_FILE, "r", encoding="utf-8") as f:
        lines = f.readlines()

    written = set()
    out = []
    for line in lines:
        stripped = line.strip()
        if "=" in stripped and not stripped.startswith("#"):
            key = stripped.split("=", 1)[0].strip()
            if key in updates:
                out.append(f"{key}={updates[key]}\n")
                written.add(key)
                continue
        out.append(line)

    # Добавить ключи, которых не было
    for key, val in updates.items():
        if key not in written:
            out.append(f"{key}={val}\n")

    with open(ENV_FILE, "w", encoding="utf-8") as f:
        f.writelines(out)
    return True


def optimize(env: dict, stats_7d: dict, stats_all: dict) -> tuple[dict, list]:
    """
    Осторожная оптимизация мягких параметров на основе реальных метрик.
    Возвращает (updates, changes_log).
    """
    updates = {}
    changes = []

    wr = stats_7d["winrate"]
    pf = stats_7d["profit_factor"]
    sl_rate = 0.0
    # доля стопов = убыточные / всего закрытых
    if stats_7d["count"] > 0:
        sl_rate = stats_7d["losses"] / stats_7d["count"] * 100

    # Текущие значения
    leverage = get_num(env, "LEVERAGE", 2)
    qty      = get_num(env, "QTY_USDT", 20)
    tp_mult  = get_num(env, "ATR_TP_MULT", 2.0)
    sl_mult  = get_num(env, "ATR_SL_MULT", 1.0)
    dll      = get_num(env, "DAILY_LOSS_LIMIT", -50)

    # ── 1. Гигиена: привести LEVERAGE к целому в пределах ──
    lev_clamped = clamp("LEVERAGE", leverage)
    if str(lev_clamped) != env.get("LEVERAGE", ""):
        updates["LEVERAGE"] = lev_clamped
        changes.append(f"🔧 LEVERAGE нормализован: {env.get('LEVERAGE')} → {lev_clamped}")

    # ── 2. Гигиена: DAILY_LOSS_LIMIT не мягче -50 ──
    dll_clamped = clamp("DAILY_LOSS_LIMIT", dll)
    if dll_clamped != dll:
        updates["DAILY_LOSS_LIMIT"] = dll_clamped
        changes.append(f"🔒 DAILY_LOSS_LIMIT приведён к пределу: {dll} → {dll_clamped}")

    # Дальше — только при достаточной статистике
    if stats_7d["count"] < MIN_TRADES:
        changes.append(f"ℹ️  Сделок за 7д: {stats_7d['count']} (<{MIN_TRADES}) — тонкая настройка пропущена")
        return updates, changes

    # ── 3. Много стопов → мягче SL, шире TP ──
    if sl_rate > 60:
        new_sl = clamp("ATR_SL_MULT", sl_mult + STEP["ATR_SL_MULT"])
        if new_sl != sl_mult:
            updates["ATR_SL_MULT"] = new_sl
            changes.append(f"📉 Стопов {sl_rate:.0f}% → ATR_SL_MULT {sl_mult} → {new_sl} (шире стоп)")
        new_tp = clamp("ATR_TP_MULT", tp_mult + STEP["ATR_TP_MULT"])
        if new_tp != tp_mult:
            updates["ATR_TP_MULT"] = new_tp
            changes.append(f"📉 → ATR_TP_MULT {tp_mult} → {new_tp} (больше потенциал)")

    # ── 4. Низкий winrate → снизить плечо и размер ──
    if wr < 40:
        new_lev = clamp("LEVERAGE", lev_clamped - 1)
        if new_lev != lev_clamped:
            updates["LEVERAGE"] = new_lev
            changes.append(f"⚠️ WR {wr:.0f}% → LEVERAGE {lev_clamped} → {new_lev} (снижаем риск)")
        new_qty = clamp("QTY_USDT", qty - STEP["QTY_USDT"])
        if new_qty != qty:
            updates["QTY_USDT"] = new_qty
            changes.append(f"⚠️ → QTY_USDT {qty} → {new_qty} (меньше размер)")

    # ── 5. Отличный результат → аккуратно нарастить размер ──
    elif wr > 60 and pf > 1.5:
        new_qty = clamp("QTY_USDT", qty + STEP["QTY_USDT"])
        if new_qty != qty:
            updates["QTY_USDT"] = new_qty
            changes.append(f"📈 WR {wr:.0f}% PF {pf} → QTY_USDT {qty} → {new_qty} (наращиваем)")

    return updates, changes


def generate_report(stats_7d, stats_all, changes) -> Path:
    """Сохранить человекочитаемый отчёт."""
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    now = datetime.now()
    report_file = REPORTS_DIR / f"optimization_{now.strftime('%Y-%m-%d')}.txt"

    lines = [
        "=" * 60,
        f"  ОТЧЁТ ОПТИМИЗАЦИИ — {now.strftime('%Y-%m-%d %H:%M:%S')}",
        "=" * 60,
        "",
        "МЕТРИКИ ЗА 7 ДНЕЙ:",
        f"  Сделок:          {stats_7d['count']}",
        f"  Winrate:         {stats_7d['winrate']}%",
        f"  Profit Factor:   {stats_7d['profit_factor']}",
        f"  PnL:             {stats_7d['pnl']:+.4f} USDT",
        f"  Max Drawdown:    -{stats_7d['max_drawdown']} USDT",
        f"  Боковик:         {stats_7d['sideways_pnl']:+.2f}$ (WR {stats_7d['sideways_wr']}%)",
        f"  Тренд:           {stats_7d['trend_pnl']:+.2f}$ (WR {stats_7d['trend_wr']}%)",
        "",
        "ЗА ВСЁ ВРЕМЯ:",
        f"  Сделок:          {stats_all['count']}",
        f"  PnL:             {stats_all['pnl']:+.4f} USDT",
        "",
    ]

    if changes:
        lines.append("ИЗМЕНЕНИЯ:")
        for c in changes:
            lines.append(f"  {c}")
    else:
        lines.append("ИЗМЕНЕНИЯ: нет — параметры в норме.")

    lines += ["", "=" * 60, ""]

    report = "\n".join(lines)
    with open(report_file, "w", encoding="utf-8") as f:
        f.write(report)
    print(report)
    return report_file


def main():
    print(f"\n🔍 [{datetime.now():%Y-%m-%d %H:%M:%S}] Умный оптимизатор запущен")
    print(f"   BOT_DIR: {BOT_DIR}")

    if not ENV_FILE.exists():
        print(f"❌ .env не найден: {ENV_FILE} — выход")
        return

    # Реальные метрики из trades.json
    stats_7d  = analytics.last_n_days(7)
    stats_all = analytics.all_time()
    print(f"📊 Сделок за 7д: {stats_7d['count']} | за всё время: {stats_all['count']}")

    env = read_env()
    updates, changes = optimize(env, stats_7d, stats_all)

    if updates:
        ok = write_env(updates)
        if ok:
            print(f"✅ Записано в .env: {updates}")
            print("⚠️  Изменения применятся при следующем перезапуске бота "
                  "(или живой adapter подхватит часть на лету).")
    else:
        print("✅ Изменений нет — параметры в пределах нормы.")

    generate_report(stats_7d, stats_all, changes)


if __name__ == "__main__":
    main()
