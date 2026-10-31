"""
edge_gate.py — правила «не торговать вхолостую и не отдавать всё комиссиям».

Идея: бот имеет право торговать реальными деньгами только пока есть статистическое доказательство, что ожидаемая прибыль на сделку ПОСЛЕ издержек положительна.
Иначе он торгует «на бумаге» (PAPER): те же сигналы и те же правила, но без реальных ордеров.

Четыре независимых слоя защиты:
  1. cost_gate       — до входа: цель обязана быть кратно больше издержек круга сделки;
  2. FeeBudget       — суточный бюджет комиссий и лимит числа сделок;
  3. evidence_ok     — «доказательство»: нижняя доверительная граница средней прибыли на сделку > 0;
  4. ModeController  — PAPER → LIVE только по доказательству; LIVE → PAPER при просадке
                       или когда статистически подтверждено, что стратегия убыточна.

Модуль чистый (stdlib), не ходит в сеть и не трогает биржу.
Все доходности — в долях от размера позиции (0.01 = 1%).
"""
import json
import math
import os
import time
from collections import deque

TAKER_FEE = 0.00055    # Bybit linear, за сторону
MAKER_FEE = 0.0002
Z_ONE_SIDED_95 = 1.645


# ═══════════════════════════════════════════════════
#   1. СТОИМОСТЬ СДЕЛКИ И ВОРОТА ДО ВХОДА
# ═══════════════════════════════════════════════════

def round_trip_costs(entry_maker=False, tp_maker=False, slip=0.0002):
    """Издержки круга сделки (доля от позиции) для двух исходов: (cost_tp, cost_sl).
    Проскальзывание начисляется только на рыночные (taker) ноги. Стоп всегда рыночный."""
    f_entry = MAKER_FEE if entry_maker else TAKER_FEE
    s_entry = 0.0 if entry_maker else slip
    f_tp = MAKER_FEE if tp_maker else TAKER_FEE
    s_tp = 0.0 if tp_maker else slip
    cost_tp = f_entry + s_entry + f_tp + s_tp
    cost_sl = f_entry + s_entry + TAKER_FEE + slip
    return cost_tp, cost_sl


def breakeven_winrate(tp_pct, sl_pct, cost_tp, cost_sl):
    """Минимальный винрейт, при котором ожидание сделки = 0 (после издержек)."""
    win = tp_pct - cost_tp
    loss = sl_pct + cost_sl
    if win <= 0:
        return 1.0   # цель не покрывает даже издержки: безубыточности не существует
    return loss / (win + loss)


def expected_value_pct(p_win, tp_pct, sl_pct, cost_tp, cost_sl):
    return p_win * (tp_pct - cost_tp) - (1 - p_win) * (sl_pct + cost_sl)


def cost_gate(tp_pct, sl_pct, entry_maker=False, tp_maker=False, slip=0.0002,
              min_tp_cost_ratio=8.0, max_breakeven_wr=0.45):
    """Ворота ДО входа. Возвращает (ok, reason).
    Блокирует сделки, у которых цель мала относительно издержек или безубыточный винрейт
    слишком высок — то есть «торговлю ради комиссии»."""
    cost_tp, cost_sl = round_trip_costs(entry_maker, tp_maker, slip)
    if tp_pct < min_tp_cost_ratio * cost_tp:
        return False, (f"цель {tp_pct:.2%} < {min_tp_cost_ratio:g}× издержек "
                       f"({cost_tp:.3%}) — комиссия съест прибыль")
    be = breakeven_winrate(tp_pct, sl_pct, cost_tp, cost_sl)
    if be > max_breakeven_wr:
        return False, f"безубыточный винрейт {be:.0%} > {max_breakeven_wr:.0%}"
    return True, f"ok: цель {tp_pct / cost_tp:.1f}× издержек, безубыточность {be:.0%}"


# ═══════════════════════════════════════════════════
#   2. БЮДЖЕТ КОМИССИЙ И ЛИМИТ ЧИСЛА СДЕЛОК
# ═══════════════════════════════════════════════════

class FeeBudget:
    """Скользящее окно 24 ч: суммарные комиссии (доля от капитала) и число сделок."""

    def __init__(self, max_fee_pct_of_equity=0.0015, max_trades=12, window_sec=86400):
        self.max_fee = max_fee_pct_of_equity
        self.max_trades = max_trades
        self.window = window_sec
        self._events = deque()   # (ts, fee_pct_of_equity)

    def _trim(self, now):
        while self._events and now - self._events[0][0] > self.window:
            self._events.popleft()

    def record(self, fee_pct_of_equity, now=None):
        now = time.time() if now is None else now
        self._events.append((now, fee_pct_of_equity))
        self._trim(now)

    def allowed(self, planned_fee_pct_of_equity, now=None):
        now = time.time() if now is None else now
        self._trim(now)
        spent = sum(f for _, f in self._events)
        if len(self._events) >= self.max_trades:
            return False, f"лимит сделок за 24 ч ({self.max_trades})"
        if spent + planned_fee_pct_of_equity > self.max_fee:
            return False, f"бюджет комиссий {self.max_fee:.3%} капитала/сутки исчерпан"
        return True, "ok"


# ═══════════════════════════════════════════════════
#   3. ДОКАЗАТЕЛЬСТВО EDGE
# ═══════════════════════════════════════════════════

def _mean_sd(xs):
    n = len(xs)
    m = sum(xs) / n
    if n < 2:
        return m, 0.0
    var = sum((x - m) ** 2 for x in xs) / (n - 1)
    return m, math.sqrt(var)


def wilson_lower(wins, n, z=Z_ONE_SIDED_95):
    """Нижняя граница доверительного интервала винрейта (Уилсон)."""
    if n == 0:
        return 0.0
    p = wins / n
    denom = 1 + z * z / n
    centre = p + z * z / (2 * n)
    margin = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return (centre - margin) / denom


def expectancy_bounds(pnl_pcts, z=Z_ONE_SIDED_95):
    """(mean, lower, upper) средней прибыли на сделку (доля от позиции) с односторонней
    нормальной оценкой. При тяжёлых хвостах оценка оптимистична — поэтому min_trades большой."""
    n = len(pnl_pcts)
    if n == 0:
        return 0.0, float("-inf"), float("inf")
    m, sd = _mean_sd(pnl_pcts)
    se = sd / math.sqrt(n) if n > 1 else float("inf")
    return m, m - z * se, m + z * se


def evidence_ok(pnl_pcts, min_trades=150, z=Z_ONE_SIDED_95, min_mean_pct=0.0):
    """Есть ли доказательство прибыльности: нижняя граница среднего > min_mean_pct.
    Возвращает (ok, reason, stats)."""
    n = len(pnl_pcts)
    m, lo, hi = expectancy_bounds(pnl_pcts, z)
    st = {"n": n, "mean": m, "lower": lo, "upper": hi}
    if n < min_trades:
        return False, f"мало сделок: {n} < {min_trades}", st
    if lo <= min_mean_pct:
        return False, (f"нижняя граница среднего {lo:+.3%}/сделку ≤ {min_mean_pct:+.3%} "
                       f"(среднее {m:+.3%}) — edge не доказан"), st
    return True, f"edge доказан: среднее {m:+.3%}/сделку, нижняя граница {lo:+.3%}", st


# ═══════════════════════════════════════════════════
#   4. РЕЖИМЫ PAPER / LIVE
# ═══════════════════════════════════════════════════

class ModeController:
    """Хранит режим в файле. По умолчанию — PAPER (безопасно).
    PAPER → LIVE: evidence_ok на paper-сделках текущей версии стратегии.
    LIVE → PAPER: просадка ≥ max_dd_pct ИЛИ на последних demote_window живых сделках
    верхняя граница среднего < 0 (статистически подтверждённый убыток)."""

    def __init__(self, path="logs/edge_state.json", promote_min=150, demote_window=50,
                 max_dd_pct=0.03, z=Z_ONE_SIDED_95):
        self.path = path
        self.promote_min = promote_min
        self.demote_window = demote_window
        self.max_dd_pct = max_dd_pct
        self.z = z

    def state(self):
        try:
            with open(self.path, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {"mode": "PAPER", "since": time.time(), "reason": "start (безопасный режим по умолчанию)"}

    def _save(self, mode, reason):
        st = {"mode": mode, "since": time.time(), "reason": reason}
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(st, f, ensure_ascii=False)
        os.replace(tmp, self.path)
        return st

    def mode(self):
        return self.state()["mode"]

    def allows_live(self):
        return self.mode() == "LIVE"

    def evaluate(self, paper_pnl_pcts, live_pnl_pcts, equity_dd_pct=0.0):
        """Пересчитать режим. Возвращает (режим, причина, сменился_ли)."""
        cur = self.mode()
        if cur == "LIVE":
            if equity_dd_pct >= self.max_dd_pct:
                st = self._save("PAPER", f"просадка {equity_dd_pct:.2%} ≥ {self.max_dd_pct:.2%}")
                return "PAPER", st["reason"], True
            recent = live_pnl_pcts[-self.demote_window:]
            if len(recent) >= self.demote_window:
                m, _lo, hi = expectancy_bounds(recent, self.z)
                if hi < 0:
                    st = self._save("PAPER", f"убыток подтверждён: среднее {m:+.3%}, верхняя граница {hi:+.3%}")
                    return "PAPER", st["reason"], True
            return "LIVE", self.state()["reason"], False

        ok, reason, _ = evidence_ok(paper_pnl_pcts, self.promote_min, self.z)
        if ok:
            st = self._save("LIVE", reason)
            return "LIVE", st["reason"], True
        return "PAPER", reason, False
