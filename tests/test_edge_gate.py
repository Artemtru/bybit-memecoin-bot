"""Тесты edge_gate.py (stdlib, без сети). Запуск из корня репозитория: python tests/test_edge_gate.py"""
import os, sys, tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import edge_gate as g

# ── издержки ──────────────────────────────────────────────────────────────────
tp, sl = g.round_trip_costs(False, False, 0.0002)
assert abs(tp - 0.0015) < 1e-12 and abs(sl - 0.0015) < 1e-12, (tp, sl)       # taker/taker: 0.15%
tp, sl = g.round_trip_costs(True, True, 0.0002)
assert abs(tp - 0.0004) < 1e-12 and abs(sl - 0.00095) < 1e-12, (tp, sl)       # maker вход+тейк, стоп рыночный

# ── безубыточный винрейт: тейк 1%, стоп 0.5%, taker 0.15% → 0.0065/0.015 = 43.3% ──
be = g.breakeven_winrate(0.01, 0.005, 0.0015, 0.0015)
assert abs(be - 0.43333) < 1e-4, be
assert g.breakeven_winrate(0.001, 0.005, 0.0015, 0.0015) == 1.0               # цель не покрывает издержки
assert abs(g.expected_value_pct(be, 0.01, 0.005, 0.0015, 0.0015)) < 1e-12     # в точке безубыточности EV = 0

# ── ворота до входа ────────────────────────────────────────────────────────────────
ok, why = g.cost_gate(0.01, 0.005)                       # цель 1% при taker: 6.7× издержек < 8×
assert not ok and "издержек" in why, why
ok, why = g.cost_gate(0.005, 0.0025, entry_maker=True, tp_maker=True)  # 0.5% цель, но maker: 12.5× → ок
assert ok, why
ok, why = g.cost_gate(0.02, 0.01)                        # крупная цель при taker: 13×, безубыточность ~38%
assert ok, why
ok, why = g.cost_gate(0.02, 0.05)                        # стоп в 2.5 раза больше цели → безубыточный WR слишком высок
assert not ok and "винрейт" in why, why

# ── Уилсон ────────────────────────────────────────────────────────────────────────
w = g.wilson_lower(40, 100)
assert 0.31 < w < 0.34, w
assert g.wilson_lower(0, 0) == 0.0

# ── доказательство edge ────────────────────────────────────────────────────────────────
good  = [0.004 if i % 3 else -0.003 for i in range(300)]    # среднее +0.17%
noise = [0.004 if i % 2 == 0 else -0.004 for i in range(300)]  # среднее 0
bad   = [0.004 if i % 4 == 0 else -0.003 for i in range(100)]  # среднее -0.125%
ok, why, st = g.evidence_ok(good, min_trades=150)
assert ok and st["lower"] > 0, (why, st)
ok, why, st = g.evidence_ok(noise, min_trades=150)
assert not ok, why
ok, why, st = g.evidence_ok(good[:50], min_trades=150)
assert not ok and "мало сделок" in why, why
assert g.expectancy_bounds([])[1] == float("-inf")

# ── бюджет комиссий ──────────────────────────────────────────────────────────────────
fb = g.FeeBudget(max_fee_pct_of_equity=0.0015, max_trades=3)
fb.record(0.0005, now=1000); fb.record(0.0005, now=1100)
assert fb.allowed(0.0004, now=1200)[0]                        # 0.0014 ≤ 0.0015
ok, why = fb.allowed(0.0006, now=1200)                        # 0.0016 > 0.0015
assert not ok and "бюджет" in why, why
assert fb.allowed(0.0006, now=1000 + 90000)[0]                # окно 24 ч истекло
fb2 = g.FeeBudget(max_fee_pct_of_equity=1.0, max_trades=2)
fb2.record(0.0001, now=1); fb2.record(0.0001, now=2)
ok, why = fb2.allowed(0.0001, now=3)
assert not ok and "лимит сделок" in why, why

# ── контроллер режимов ──────────────────────────────────────────────────────────────────
with tempfile.TemporaryDirectory() as d:
    mc = g.ModeController(path=os.path.join(d, "state.json"), promote_min=150, demote_window=50, max_dd_pct=0.03)
    assert mc.mode() == "PAPER" and not mc.allows_live()                        # безопасный старт
    m, why, changed = mc.evaluate(noise, [], 0.0)
    assert m == "PAPER" and not changed, why                                    # доказательства нет
    m, why, changed = mc.evaluate(good, [], 0.0)
    assert m == "LIVE" and changed and mc.allows_live(), why                    # доказательство есть → LIVE
    m, why, changed = mc.evaluate(good, good[:50], 0.0)
    assert m == "LIVE" and not changed, why                                     # живые сделки хорошие
    m, why, changed = mc.evaluate(good, bad[:50], 0.0)
    assert m == "PAPER" and changed and "убыток" in why, why                    # подтверждённый убыток → PAPER
    mc.evaluate(good, [], 0.0)                                                   # снова LIVE
    assert mc.allows_live()
    m, why, changed = mc.evaluate(good, good[:50], 0.04)
    assert m == "PAPER" and changed and "просадка" in why, why                  # просадка → PAPER
    # состояние переживает «перезапуск»
    mc2 = g.ModeController(path=os.path.join(d, "state.json"))
    assert mc2.mode() == "PAPER"

print("ALL EDGE_GATE TESTS PASSED")
