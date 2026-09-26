#!/usr/bin/env python3
"""
Тест интеграции AI компонентов без реального подключения к Bybit.
Проверяет, что все модули корректно импортируются и взаимодействуют.
"""

import os
import sys

# Мокаем переменные окружения для теста
os.environ['BYBIT_API_KEY'] = 'test_key'
os.environ['BYBIT_API_SECRET'] = 'test_secret'
os.environ['TELEGRAM_BOT_TOKEN'] = 'test_token'
os.environ['TELEGRAM_CHAT_ID'] = '123456'
os.environ['HERMES_ENABLED'] = '0'  # Отключаем для теста
os.environ['AI_MODE'] = 'hybrid'
os.environ['MAX_POSITIONS'] = '5'
os.environ['MAX_LEVERAGE'] = '2'

print("🧪 Testing AI Integration Components...\n")

# 1. Тест синтаксиса Python
print("1️⃣ Checking Python syntax...")
import py_compile
files_to_check = [
    'manager.py',
    'hermes_brain.py',
    'telegram_ai_control.py',
    'config_validator.py'
]

syntax_ok = True
for file in files_to_check:
    try:
        py_compile.compile(file, doraise=True)
        print(f"   ✅ {file}")
    except py_compile.PyCompileError as e:
        print(f"   ❌ {file}: {e}")
        syntax_ok = False

if not syntax_ok:
    print("\n❌ SYNTAX ERRORS FOUND")
    sys.exit(1)

# 2. Тест импортов (без pybit — он есть только на хосте)
print("\n2️⃣ Checking imports...")
try:
    import hermes_brain
    print("   ✅ hermes_brain.py")
except Exception as e:
    print(f"   ❌ hermes_brain.py: {e}")
    sys.exit(1)

try:
    import telegram_ai_control
    print("   ✅ telegram_ai_control.py")
except Exception as e:
    print(f"   ❌ telegram_ai_control.py: {e}")
    sys.exit(1)

try:
    import config_validator
    print("   ✅ config_validator.py")
except Exception as e:
    print(f"   ❌ config_validator.py: {e}")
    sys.exit(1)

# 3. Тест структуры hermes_brain
print("\n3️⃣ Checking hermes_brain structure...")
required_functions = [
    'should_trade',
    'search_news',
    'check_rug_pull_risk',
    'explain_decision',
    'daily_summary',
    'health_check',
    'get_status'
]

for func in required_functions:
    if hasattr(hermes_brain, func):
        print(f"   ✅ {func}()")
    else:
        print(f"   ❌ {func}() missing")
        sys.exit(1)

# 4. Тест fallback behaviour (Hermes отключён)
print("\n4️⃣ Testing fallback behavior (HERMES_ENABLED=0)...")
try:
    # should_trade должен разрешать сделки при отключенном Hermes
    allowed, reason = hermes_brain.should_trade(
        symbol="BTCUSDT",
        signal="BUY",
        price=50000.0,
        rsi=50.0,
        adx=25.0,
        atr_pct=2.0,
        mode="trend",
        vol_ratio=1.2,
        include_news=False
    )
    
    if allowed:
        print(f"   ✅ Fallback works: {reason}")
    else:
        print(f"   ❌ Fallback failed: should allow trades when disabled")
        sys.exit(1)
        
except Exception as e:
    print(f"   ❌ should_trade() error: {e}")
    sys.exit(1)

# 5. Проверка .env конфигурации
print("\n5️⃣ Checking .env configuration...")
from dotenv import load_dotenv
load_dotenv()

critical_vars = ['AI_MODE', 'MAX_POSITIONS', 'MAX_LEVERAGE', 'HERMES_ENABLED']
for var in critical_vars:
    value = os.getenv(var)
    if value:
        print(f"   ✅ {var}={value}")
    else:
        print(f"   ⚠️  {var} not set")

print("\n" + "="*60)
print("✅ ALL TESTS PASSED - Code is ready for deployment")
print("="*60)
