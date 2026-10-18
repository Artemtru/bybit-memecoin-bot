"""Регрессионный тест: размер позиции должен следовать QTY_USDT.

Баг (исправлен 2026-10-18): atr_pct передавался в ПРОЦЕНТАХ (0.27 == 0.27%),
а формула ждала долю, поэтому размер всегда падал в MIN_POSITION_USDT.

Функции достаются из manager.py через ast, поэтому тесту не нужны ни сеть,
ни API-ключи, ни pybit. Запуск из корня репозитория: python tests/test_sizing.py
"""
import ast
import math
from decimal import Decimal


def load(names, consts):
    src = open("manager.py", encoding="utf-8").read()
    tree = ast.parse(src)
    ns = {"math": math, "Decimal": Decimal}
    ns.update(consts)
    found = set()
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in names:
            mod = ast.Module(body=[node], type_ignores=[])
            exec(compile(mod, "manager.py", "exec"), ns)
            found.add(node.name)
    missing = set(names) - found
    assert not missing, f"functions not found in manager.py: {missing}"
    return ns


ns = load(
    {"calc_qty", "calc_adaptive_qty"},
    {"QTY_USDT_BASE": 15.0, "MIN_POSITION_USDT": 10.0, "MAX_POSITION_USDT": 30.0},
)


def notional(price, atr_pct, step, min_qty):
    qty = float(ns["calc_adaptive_qty"](price, atr_pct, step, min_qty))
    return qty * price


# Тихая монета: ATR 0.27% (в процентах, как отдаёт detect_market_mode) —
# позиция должна быть ≈ QTY_USDT (15$), а не 10$ (MIN_POSITION_USDT).
n_quiet = notional(0.0465, 0.27, 1.0, 1.0)
assert 13.5 <= n_quiet <= 15.5, f"тихая монета: ожидалось ~15$, получено {n_quiet:.2f}$"

# Волатильная монета (ATR 5%): позиция уменьшается, но не ниже MIN_POSITION_USDT.
n_wild = notional(0.0465, 5.0, 1.0, 1.0)
assert 9.5 <= n_wild <= 11.0, f"волатильная монета: ожидалось ~10$, получено {n_wild:.2f}$"
assert n_wild < n_quiet, "волатильная монета должна получать меньшую позицию"

# Нет данных по ATR → безопасный дефолт, без исключений и без выхода за границы.
n_none = notional(0.21, None, 1.0, 1.0)
assert 10.0 - 0.5 <= n_none <= 30.0, f"без ATR: размер вне границ: {n_none:.2f}$"

# Минимальный лот не должен нарушаться.
q = float(ns["calc_adaptive_qty"](5000.0, 0.3, 0.001, 0.001))
assert q >= 0.001, f"qty ниже минимального лота: {q}"

print(f"OK: quiet={n_quiet:.2f}$ wild={n_wild:.2f}$ no_atr={n_none:.2f}$")
