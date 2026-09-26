# 🤖 Bybit Memecoin Bot с AI (Hermes)

Автоматическая торговля мемкоинами на Bybit Futures с интеграцией AI агента для принятия решений и управления через Telegram.

## 🌟 Возможности

### Режим Б — Гибридный (Hybrid AI Mode)

✅ **Автоматическая торговля** — сканирование 12 мемкоинов (DOGE, PEPE, SHIB, FLOKI, BONK и др.)  
✅ **AI вето** — каждая сделка проверяется через Hermes AI перед исполнением  
✅ **Анализ новостей** — проверка новостей и Twitter перед входом в позицию  
✅ **Telegram управление** — команды в свободной форме через `/ask`  
✅ **Жёсткие лимиты** — AI не может нарушить границы безопасности  
✅ **Подтверждение** — критичные операции требуют одобрения пользователя  

## 📊 Архитектура

```
manager.py              → Главный цикл: скан монет, анализ RSI/BB/ADX/ATR
  ├─ hermes_brain.py    → AI модуль: should_trade(), search_news(), rug_pull_risk
  ├─ scanner.py         → Bybit API: цены, объёмы, индикаторы
  └─ analytics.py       → Логирование сделок, отчёты PnL

telegram_commands.py    → Telegram бот: /status, /pnl, /ask <команда>
  └─ telegram_ai_control.py  → AI интерпретация команд, валидация лимитов

config_validator.py     → Проверка конфигурации при старте
```

## 🚀 Быстрый старт

### 1. Установка зависимостей

```bash
cd /opt/data/bybit-memecoin-bot
pip install -r requirements.txt
```

### 2. Настройка конфигурации

Скопируйте `.env.production` в `.env` и заполните:

```bash
cp .env.production .env
nano .env
```

**Обязательные параметры:**
```env
BYBIT_API_KEY=ваш_ключ
BYBIT_API_SECRET=ваш_секрет
TELEGRAM_TOKEN=токен_бота
TELEGRAM_CHAT_ID=ваш_chat_id

HERMES_ENABLED=1
HERMES_API_URL=http://localhost:8642/v1/chat/completions
AI_MODE=hybrid
```

### 3. Проверка конфигурации

```bash
python config_validator.py .env
```

Должно вывести: `✅ Конфигурация корректна. Все проверки пройдены.`

### 4. Запуск

```bash
# Главный менеджер (торговля)
python manager.py &

# Telegram команды (в отдельном терминале)
python telegram_commands.py &
```

## 💬 Telegram команды

### Базовые команды

```
/status        — общий статус (позиции, PnL, лимиты)
/coins         — детали по каждой монете (RSI, ATR, режим рынка)
/pnl           — текущий PnL за день
/top           — свежий скан топ-монет прямо сейчас
/help          — список всех команд
```

### Аналитика

```
/daily         — отчёт за сегодня
/weekly        — отчёт за неделю
/monthly       — отчёт за месяц
```

### AI Управление (режим Б)

Команды в **свободной форме** через `/ask`:

#### Примеры управления позициями

```
/ask Закрой все позиции
/ask Закрой только DOGE
/ask Какие позиции сейчас открыты?
```

#### Примеры изменения параметров

```
/ask Увеличь размер позиции до 25 USDT
/ask Поставь макс 4 позиции одновременно
/ask Уменьши стоп-лосс до 3%
```

#### Примеры аналитики и советов

```
/ask Какие монеты сейчас перспективны?
/ask Стоит ли входить в PEPE?
/ask Проанализируй рынок мемкоинов
/ask Есть ли риски у BONK?
```

#### Примеры управления ботом

```
/ask Останови бот на час
/ask Возобнови работу
/ask Покажи текущие лимиты
```

## 🔒 Лимиты безопасности (жёсткие границы)

AI **не может** нарушить эти параметры:

| Параметр | Значение | Комментарий |
|----------|----------|-------------|
| `MAX_POSITION_USDT` | 30 USDT | Максимум на одну позицию |
| `MAX_POSITIONS` | 5 | Максимум позиций одновременно |
| `MAX_LEVERAGE` | 2x | Максимальное плечо |
| `DAILY_LOSS_LIMIT` | -20 USDT | Останов при убытке за день |

**Гибкие параметры** (AI может предлагать изменения в диапазоне):

- `QTY_USDT`: 10-30 USDT (размер позиции)
- `MAX_BOTS`: 1-5 (количество одновременных позиций)
- `STOP_LOSS_PCT`: 3-10% (стоп-лосс)
- `TAKE_PROFIT_PCT`: 5-15% (тейк-профит)

## 🧠 Как работает AI в режиме Б

### 1. Автоматическое вето перед сделкой

Когда бот видит сигнал:

```python
# scanner.py → RSI < 40, ADX > 25 → сигнал BUY DOGE
↓
# hermes_brain.should_trade() → проверка через AI:
#   - технические индикаторы корректны?
#   - search_news(DOGE) → что пишут в Twitter/новостях?
#   - check_rug_pull_risk() → есть ли признаки скама?
↓
# Результат: (True, "✅ Сделка одобрена [85%]") или
#            (False, "❌ Отклонено: негативные новости")
```

### 2. Управление через /ask

Пользователь пишет: `/ask Увеличь размер позиции до 25 USDT`

```python
# telegram_commands.py → принимает команду
↓
# telegram_ai_control.process_command() → AI разбирает команду:
#   → action: "set_param"
#   → params: {"position_size": 25}
#   → needs_confirmation: false (в пределах лимитов)
↓
# Валидация: 10 <= 25 <= 30 ✓
↓
# Выполнение: manager.set_parameter("position_size", 25)
↓
# Telegram: ✅ Параметр position_size установлен: 25
```

### 3. Подтверждение критичных операций

Пользователь пишет: `/ask Закрой все позиции`

```python
# AI определяет: action="close_all", needs_confirmation=true
↓
# Telegram отправляет с кнопками:
#   ⚠️ Требуется подтверждение:
#   Закрыть все 3 открытые позиции (текущий PnL: +5.23 USDT)?
#   [✅ Да]  [❌ Нет]
↓
# Пользователь нажимает ✅ Да
↓
# execute_action() → manager.close_all_positions()
```

## 📈 Индикаторы и условия входа

### Технический анализ

- **RSI(14)**: < 40 → BUY, > 60 → SELL
- **Bollinger Bands(20, 2σ)**: отскок от нижней/верхней границы
- **ADX(14)**: > 25 → сильный тренд, иначе боковик
- **ATR(14)**: волатильность для расчёта стоп-лосса

### Режимы рынка

1. **Тренд** (ADX > 25):
   - Вход: RSI < 40/> 60 + цена за BB границей + объём > 1.5x
   - AI проверяет: "тренд устойчивый? новости подтверждают?"

2. **Боковик** (ADX < 25):
   - Вход: RSI экстремумы (< 35 или > 65)
   - AI проверяет: "это не ловушка? нет rug pull рисков?"

### Проверка новостей (AI)

Перед каждой сделкой:
```python
news = search_news(symbol, lookback_hours=24)
# → sentiment: -1.0..+1.0 (bearish → bullish)
# → red_flags: ["rug pull concerns", "whale dump", ...]
# → sources: количество найденных источников
```

AI отклоняет сделку если:
- `sentiment < -0.5` (сильно негативные новости)
- `red_flags не пуст` (тревожные сигналы)
- `sources == 0` и волатильность > 15% (подозрительная активность без новостей)

## 🛠️ Разработка и деплой

### Структура проекта

```
bybit-memecoin-bot/
├── manager.py                 # главный цикл торговли
├── scanner.py                 # Bybit API, индикаторы
├── analytics.py               # логирование, отчёты
├── hermes_brain.py            # AI модуль ⭐
├── telegram_commands.py       # Telegram бот
├── telegram_ai_control.py     # AI команды ⭐
├── config_validator.py        # валидация конфигурации
├── .env.production            # шаблон конфигурации
├── requirements.txt           # зависимости
├── logs/
│   ├── bot.log               # основной лог
│   ├── status.json           # текущий статус для Telegram
│   └── trades.jsonl          # история сделок
└── README.md                  # эта документация
```

### Деплой на VPS

**Через GitHub Actions (рекомендуется):**

1. Создайте секреты в GitHub:
   - `VPS_HOST`: IP адрес VPS
   - `SSH_PRIVATE_KEY`: приватный SSH ключ
   - `VPS_USER`: пользователь (обычно `root`)

2. Push в `main` → автоматический деплой:
   ```yaml
   # .github/workflows/deploy.yml уже настроен
   ```

**Вручную:**

```bash
# На VPS
cd /opt/data/bybit-memecoin-bot
git pull origin main

# Остановить старые процессы
pkill -f "python manager.py"
pkill -f "python telegram_commands.py"

# Проверить конфигурацию
python config_validator.py .env

# Запустить
nohup python manager.py > logs/manager.log 2>&1 &
nohup python telegram_commands.py > logs/telegram.log 2>&1 &
```

### Тестирование AI модуля

```bash
# Проверка Hermes подключения
curl -X POST http://localhost:8642/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model": "anthropic/claude-sonnet-4.5", "messages": [{"role": "user", "content": "Test"}]}'

# Тест search_news()
python -c "
from hermes_brain import search_news
result = search_news('DOGEUSDT', lookback_hours=24)
print(result)
"

# Тест AI команд
python telegram_ai_control.py
```

## 🐛 Решение проблем

### Hermes недоступен

**Проблема:** `[hermes] Hermes недоступен`

**Решение:**
```bash
# Проверить статус Hermes
docker ps | grep hermes

# Проверить порт
curl http://localhost:8642/health

# Рестарт Hermes
docker restart hermes
```

### AI отклоняет все сделки

**Проблема:** `[hermes] should_trade(DOGE BUY): trade=False, confidence=0.20, reason=...`

**Решение:**
1. Проверить логи: `tail -f logs/bot.log | grep hermes`
2. Временно отключить новости: `AI_NEWS_CHECK=false` в `.env`
3. Снизить чувствительность news sentiment в `hermes_brain.py`

### Команды /ask не работают

**Проблема:** `❌ AI агент недоступен`

**Решение:**
```bash
# Проверить HERMES_ENABLED в .env
grep HERMES_ENABLED .env

# Проверить HERMES_API_URL
grep HERMES_API_URL .env

# Тест модуля
python -c "
import telegram_ai_control
result = telegram_ai_control.process_command('Покажи статус', {})
print(result)
"
```

## 📊 Мониторинг

### Основные метрики

- **Daily PnL**: `/pnl` — PnL за день vs лимит убытков
- **Win rate**: (`/daily`) — процент прибыльных сделок
- **AI veto rate**: логи → сколько сделок отклонил AI
- **Avg hold time**: логи → среднее время в позиции

### Алерты

Бот автоматически отправляет уведомления:

✅ **При каждой сделке** (если `NOTIFY_ON_TRADE=true`):
```
📈 Открыта позиция LONG DOGE
Цена: 0.08234 | Размер: 20 USDT
RSI: 38.5 | ADX: 27.3
AI: ✅ Одобрено [82%]
```

⚠️ **При AI вето** (если `NOTIFY_ON_AI_VETO=true`):
```
❌ AI отклонил сделку LONG PEPE
Причина: Негативные новости о возможном rug pull
Sentiment: -0.73 | Источники: 5
```

🚨 **При достижении лимита** (всегда):
```
🚨 DAILY LOSS LIMIT REACHED
Текущий PnL: -20.14 USDT
Бот остановлен до завтра 00:00 UTC
```

## 📝 Лицензия

MIT License. Используйте на свой риск.

## ⚠️ Дисклеймер

Этот бот предназначен для образовательных целей. Торговля криптовалютами сопряжена с высокими рисками. Всегда:

1. Тестируйте на малых суммах
2. Используйте жёсткие лимиты (`DAILY_LOSS_LIMIT`)
3. Не инвестируйте больше, чем готовы потерять
4. Регулярно проверяйте логи и PnL

AI не является гарантией прибыли. Всегда имейте план выхода.

---

**Вопросы?** → Проверьте логи в `logs/bot.log` или создайте issue в GitHub.
