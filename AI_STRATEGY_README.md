# 🤖 AI Strategy Manager — Управление стратегией

## Что изменилось

**AI теперь не только блокирует опасные сделки (veto), но и корректирует стратегию:**
- RSI пороги (BUY/SELL)
- Размер позиций (QTY_USDT)
- Количество ботов (MAX_BOTS)
- Интервал сканирования (SCAN_INTERVAL_MIN)

## Лимиты токенов

- **2 вызова в день** (утро + вечер)
- **Минимум 12 часов** между вызовами
- Не блокирует работу бота (fail-safe)

## Новые параметры (менее консервативные)

### До:
```
QTY_USDT=20
SCAN_INTERVAL_MIN=30
MAX_BOTS=3
RSI_BUY=45
RSI_SELL=55
```

### Сейчас (агрессивный scalping):
```
QTY_USDT=30                  # +50% размер позиции
SCAN_INTERVAL_MIN=15         # в 2 раза чаще сканы
MAX_BOTS=5                   # больше параллельных позиций
RSI_BUY=35                   # раньше входим (было 45)
RSI_SELL=65                  # позже выходим (было 55)
MAX_POSITION_USDT=50         # AI может рекомендовать до 50 USDT
```

## Как это работает

### 1. AI Veto (как было)
```python
# Перед каждой сделкой — AI проверяет:
- Анализ новостей (web_search)
- Rug pull risk
- Market sentiment
- Блокирует опасные сделки
```

### 2. AI Strategy Manager (новое)
```python
# 2 раза в день AI анализирует:
- Daily PnL
- Win rate
- Количество сделок
- Волатильность рынка

# И корректирует:
- RSI пороги (если мало сигналов → расширяет диапазон)
- Размер позиций (если маленький профит → увеличивает QTY)
- Частоту сканирования (если пропускаем сигналы → ускоряет)
```

## Telegram уведомления

При корректировке стратегии AI отправит:
```
🧠 AI Strategy Adjustment
Market is volatile, widening RSI range

RSI: 30/70
Suggested QTY: 35 USDT
```

## Безопасность

**Hard limits остались:**
- MAX_LEVERAGE=2 (не может превысить)
- MAX_POSITIONS=5 (не может превысить)
- DAILY_LOSS_LIMIT=-50 (не может превысить)

**AI может корректировать только:**
- RSI пороги (20-50 для BUY, 50-80 для SELL)
- QTY_USDT (10-50 USDT)
- MAX_BOTS (1-5)
- SCAN_INTERVAL_MIN (5-60 мин)

## Логи

AI Strategy Manager пишет логи в:
```
logs/ai_strategy_cache.json  # история корректировок
logs/manager_YYYYMMDD.log    # общие логи бота
```

## Проверка статуса

Добавь команду `/ai_status` в Telegram bot для проверки:
```python
{
  "calls_today": 1,
  "remaining_calls": 1,
  "last_call": "2026-09-26T10:30:00",
  "total_adjustments": 3
}
```

## Откат к старым параметрам

Если AI слишком агрессивен — скопируй `.env.conservative`:
```bash
cp .env.conservative .env
systemctl restart memecoin-bot telegram-bot
```

## FAQ

**Q: AI будет менять параметры каждый час?**
A: Нет. 2 раза в день максимум (и только при сильных изменениях рынка).

**Q: Можно ли отключить AI Strategy Manager?**
A: Да. Установи `HERMES_ENABLED=0` в `.env` — останется только базовая логика.

**Q: AI может разорить бота?**
A: Нет. Hard limits защищают от катастрофических изменений (max leverage=2, max loss=-50 USDT).

**Q: Как проверить что AI действительно работает?**
A: 
1. Проверь логи: `journalctl -u memecoin-bot | grep "AI Strategy"`
2. Проверь файл: `cat logs/ai_strategy_cache.json`
3. Подожди 12 часов после запуска — AI сделает первую корректировку

**Q: Токены очень дорогие, можно меньше вызовов?**
A: Да. В `ai_strategy_manager.py` измени `MAX_CALLS_PER_DAY=1` (один раз в день).
