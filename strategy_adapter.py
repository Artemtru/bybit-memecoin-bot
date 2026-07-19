"""
╔══════════════════════════════════════════════════════╗
║       STRATEGY_ADAPTER.PY — Адаптация стратегии     ║
║                                                      ║
║  На основе накопленной статистики автоматически     ║
║  меняет параметры стратегии чтобы:                  ║
║  • Избежать серий потерь                            ║
║  • Усилить то, что работает                         ║
║  • Защитить капитал при просадке                    ║
╚══════════════════════════════════════════════════════╝

Вызывается из manager.py каждые N сканирований.
"""

import os
import logging
import analytics
import telegram_notifier as tg

log = logging.getLogger("adapter")

# ── Пороги для принятия решений ──────────────────────

# Минимум сделок для принятия решений
MIN_TRADES_FOR_DECISION = 10

# Profit Factor: нормальный > 1.3, плохой < 1.0
PF_GOOD    = 1.3
PF_BAD     = 1.0

# Winrate
WR_GOOD    = 55.0   # хорошо
WR_BAD     = 45.0   # плохо — надо менять что-то

# Просадка (от дневного лимита)
DD_CAUTION = 10.0   # -10 USDT → снизить размер позиции
DD_DANGER  = 15.0   # -15 USDT → стоп новых сделок до конца дня

# Сдвиги параметров за один шаг адаптации
RSI_STEP   = 2.0    # сдвиг порога RSI
BB_STD_STEP = 0.2   # сдвиг множителя σ Bollinger Bands
ATR_TP_STEP = 0.2   # сдвиг множителя ATR для TP
ATR_SL_STEP = 0.1   # сдвиг множителя ATR для SL


def _read_env(key, default):
    """Прочитать значение из .env файла."""
    env_path = ".env"
    if not os.path.exists(env_path):
        return default
    with open(env_path, "r", encoding="utf-8") as f:
        for line in f:
            if line.startswith(f"{key}="):
                val = line.strip().split("=", 1)[1]
                try:
                    return float(val)
                except ValueError:
                    return default
    return default


def _write_env(updates: dict):
    """Обновить несколько значений в .env файле."""
    env_path = ".env"
    try:
        with open(env_path, "r", encoding="utf-8") as f:
            lines = f.readlines()

        new_lines = []
        written = set()
        for line in lines:
            key = line.split("=")[0].strip()
            if key in updates:
                new_lines.append(f"{key}={updates[key]}\n")
                written.add(key)
            else:
                new_lines.append(line)

        # Добавить новые ключи которых не было в .env
        for key, val in updates.items():
            if key not in written:
                new_lines.append(f"{key}={val}\n")

        with open(env_path, "w", encoding="utf-8") as f:
            f.writelines(new_lines)
    except Exception as e:
        log.warning(f"Не удалось обновить .env: {e}")


def adapt(daily_pnl: float, rsi_buy: float, rsi_sell: float) -> dict:
    """
    Основная функция адаптации.
    Анализирует статистику и возвращает словарь изменений.

    Возвращает:
      {
        "rsi_buy": float,
        "rsi_sell": float,
        "bb_std": float,
        "atr_tp_mult": float,
        "atr_sl_mult": float,
        "block_new_trades": bool,  # заблокировать новые входы
        "changes": [str],          # список изменений для лога
      }
    """
    # Текущие параметры из .env
    bb_std      = _read_env("BB_STD",      2.0)
    atr_tp_mult = _read_env("ATR_TP_MULT", 2.0)
    atr_sl_mult = _read_env("ATR_SL_MULT", 1.0)

    changes        = []
    block_trades   = False
    params_changed = False

    # ── 1. Защита от просадки ────────────────────────
    if daily_pnl <= -DD_DANGER:
        block_trades = True
        changes.append(f"⛔ Просадка {daily_pnl:.2f}$ → новые входы заблокированы до завтра")

    elif daily_pnl <= -DD_CAUTION:
        # Ужесточаем SL (меньше риска на сделку)
        new_sl = round(max(atr_sl_mult - ATR_SL_STEP, 0.5), 1)
        if new_sl != atr_sl_mult:
            atr_sl_mult = new_sl
            changes.append(f"⚠️ Просадка {daily_pnl:.2f}$ → ATR_SL_MULT снижен до {new_sl}")
            params_changed = True

    # ── 2. Анализ последних 3 дней ───────────────────
    stats_3d = analytics.last_n_days(3)

    if stats_3d["count"] >= MIN_TRADES_FOR_DECISION:
        pf = stats_3d["profit_factor"]
        wr = stats_3d["winrate"]

        # Серия потерь → расширяем Bollinger Bands (меньше ложных сигналов)
        if pf < PF_BAD and wr < WR_BAD:
            new_bb = round(min(bb_std + BB_STD_STEP, 3.0), 1)
            if new_bb != bb_std:
                bb_std = new_bb
                changes.append(f"📉 WR={wr}% PF={pf} → BB_STD расширен до {new_bb}σ (меньше ложных сигналов)")
                params_changed = True

            # Увеличиваем TP чтобы перекрыть серию стопов
            new_tp = round(min(atr_tp_mult + ATR_TP_STEP, 3.0), 1)
            if new_tp != atr_tp_mult:
                atr_tp_mult = new_tp
                changes.append(f"📉 ATR_TP_MULT увеличен до {new_tp} (больше потенциал прибыли)")
                params_changed = True

        # Хорошая серия → можно немного сузить BB (больше входов)
        elif pf > PF_GOOD and wr > WR_GOOD:
            new_bb = round(max(bb_std - BB_STD_STEP, 1.5), 1)
            if new_bb != bb_std:
                bb_std = new_bb
                changes.append(f"📈 WR={wr}% PF={pf} → BB_STD сужен до {new_bb}σ (больше сигналов)")
                params_changed = True

            # Можно восстановить SL если был снижен
            new_sl = round(min(atr_sl_mult + ATR_SL_STEP, 1.5), 1)
            if new_sl != atr_sl_mult:
                atr_sl_mult = new_sl
                changes.append(f"📈 ATR_SL_MULT восстановлен до {new_sl}")
                params_changed = True

    # ── 3. Анализ по режиму рынка ────────────────────
    stats_7d = analytics.last_n_days(7)

    if stats_7d["count"] >= MIN_TRADES_FOR_DECISION:
        sw_pnl = stats_7d["sideways_pnl"]
        tr_pnl = stats_7d["trend_pnl"]
        sw_wr  = stats_7d["sideways_wr"]
        tr_wr  = stats_7d["trend_wr"]

        # Боковик приносит убытки → расширяем BB (реже входим)
        if sw_pnl < 0 and sw_wr < 45:
            new_bb = round(min(bb_std + BB_STD_STEP, 3.0), 1)
            if new_bb != bb_std:
                bb_std = new_bb
                changes.append(f"↔️ Боковик убыточен (PnL={sw_pnl:.2f}$) → BB_STD={new_bb}σ")
                params_changed = True

        # Тренд приносит убытки → адаптируем RSI
        if tr_pnl < 0 and tr_wr < 45:
            # Расширяем RSI-коридор (входим только в сильных зонах)
            new_buy  = round(max(rsi_buy  - RSI_STEP, 25.0), 1)
            new_sell = round(min(rsi_sell + RSI_STEP, 75.0), 1)
            if new_buy != rsi_buy or new_sell != rsi_sell:
                rsi_buy, rsi_sell = new_buy, new_sell
                changes.append(
                    f"📈 Тренд убыточен → RSI коридор расширен: BUY<{new_buy} SELL>{new_sell}"
                )
                params_changed = True

    # ── 4. Анализ лучших и худших монет (за неделю) ──
    stats_w = analytics.this_week()
    if stats_w["by_symbol"] and stats_w["count"] >= MIN_TRADES_FOR_DECISION:
        for sym, d in stats_w["by_symbol"].items():
            sym_wr = d["wins"]/d["count"]*100 if d["count"] else 0
            if d["pnl"] < -5 and sym_wr < 40 and d["count"] >= 5:
                changes.append(
                    f"⚠️ {sym} убыточен за неделю ({d['pnl']:+.2f}$, WR {sym_wr:.0f}%) — "
                    f"рекомендую убрать из MEME_KEYWORDS в scanner.py"
                )

    # ── 5. Записать изменения в .env ─────────────────
    if params_changed:
        _write_env({
            "BB_STD":      bb_std,
            "ATR_TP_MULT": atr_tp_mult,
            "ATR_SL_MULT": atr_sl_mult,
            "RSI_BUY":     rsi_buy,
            "RSI_SELL":    rsi_sell,
        })

    return {
        "rsi_buy":          rsi_buy,
        "rsi_sell":         rsi_sell,
        "bb_std":           bb_std,
        "atr_tp_mult":      atr_tp_mult,
        "atr_sl_mult":      atr_sl_mult,
        "block_new_trades": block_trades,
        "changes":          changes,
        "params_changed":   params_changed,
    }


def notify_if_changed(result: dict):
    """Отправить уведомление в Telegram если параметры изменились."""
    if not result["changes"]:
        return

    lines = ["🤖 <b>Адаптация стратегии</b>", ""]
    lines += result["changes"]
    lines += [
        "",
        f"Текущие параметры:",
        f"RSI: BUY<{result['rsi_buy']} SELL>{result['rsi_sell']}",
        f"BB: {result['bb_std']}σ | TP×{result['atr_tp_mult']} SL×{result['atr_sl_mult']}",
    ]
    if result["block_new_trades"]:
        lines.append("⛔ Новые входы: ЗАБЛОКИРОВАНЫ до завтра")

    tg.send_message("\n".join(lines))
    for change in result["changes"]:
        log.info(f"[adapter] {change}")
