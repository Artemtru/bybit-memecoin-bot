# 🐱 Bybit Memecoin Trading Bot

Автоматическая торговая система для фьючерсов Bybit.  
Специализируется на волатильных мемкоинах с адаптивной стратегией.

## Архитектура

```
scanner.py            ← сканирует рынок, находит топ-монеты
manager.py            ← главный процесс, управляет ботами
pnl_tracker.py        ← записывает каждую сделку в базу
analytics.py          ← считает статистику по периодам
strategy_adapter.py   ← адаптирует стратегию на основе данных
telegram_notifier.py  ← отправляет уведомления о сделках
telegram_commands.py  ← принимает команды из Telegram
```

## Стратегия

| Режим рынка | Сигнал входа | TP / SL |
|---|---|---|
| Боковик (sideways) | Bollinger Bands отскок + RSI фильтр | ATR × 2 / ATR × 1 |
| Тренд (trend) | Адаптивный RSI | ATR × 2 / ATR × 1 |

**Автоадаптация:**
- Пороги RSI подстраиваются под средний RSI рынка
- BB_STD меняется при серии потерь / побед
- При просадке −10$ ужесточается SL
- При просадке −15$ блокируются новые входы до следующего дня
- Каждый день в полночь счётчики сбрасываются

## Быстрый старт

### 1. Клонировать репозиторий
```bash
git clone https://github.com/YOUR_USERNAME/bybit-memecoin-bot.git
cd bybit-memecoin-bot
```

### 2. Виртуальное окружение
```bash
python3 -m venv venv
source venv/bin/activate   # Linux/Mac
# venv\Scripts\activate    # Windows
pip install -r requirements.txt
```

### 3. Конфигурация
```bash
cp .env.example .env
nano .env   # вписать API-ключи и токен Telegram
```

### 4. Запуск
```bash
# Основной менеджер
python manager.py

# Telegram-бот (в отдельном окне / сессии)
python telegram_commands.py
```

## Деплой на сервер (systemd)

```bash
# Основной бот
sudo nano /etc/systemd/system/memecoin-bot.service

# Telegram-бот
sudo nano /etc/systemd/system/telegram-bot.service

sudo systemctl daemon-reload
sudo systemctl enable memecoin-bot telegram-bot
sudo systemctl start memecoin-bot telegram-bot
```

Пример `.service` файла:
```ini
[Unit]
Description=Bybit Trading Bot Manager
After=network.target

[Service]
Type=simple
User=YOUR_USER
WorkingDirectory=/home/YOUR_USER/bybit-memecoin-bot
ExecStart=/home/YOUR_USER/bybit-memecoin-bot/venv/bin/python manager.py
Restart=on-failure
RestartSec=10
StandardOutput=append:/home/YOUR_USER/bybit-memecoin-bot/logs/service.log
StandardError=append:/home/YOUR_USER/bybit-memecoin-bot/logs/service-error.log

[Install]
WantedBy=multi-user.target
```

## Telegram команды

| Команда | Описание |
|---|---|
| `/status` | Общий статус системы |
| `/coins` | Детали по каждой монете (цена, RSI, ATR, режим) |
| `/pnl` | PnL за текущий день |
| `/top` | Свежий скан топ-монет рынка |
| `/daily` | Отчёт за сегодня |
| `/weekly` | Отчёт за неделю |
| `/monthly` | Отчёт за месяц |
| `/quarterly` | Отчёт за квартал |
| `/yearly` | Отчёт за год |
| `/alltime` | Отчёт за всё время |

## Структура логов

```
logs/
  manager_YYYYMMDD.log   ← основной лог торговли
  trades.json            ← база всех сделок
  status.json            ← текущий статус (читает Telegram-бот)
  service.log            ← stdout systemd
  service-error.log      ← stderr systemd (INFO тоже сюда)
```

## Безопасность

- `.env` добавлен в `.gitignore` — ключи никогда не попадут в репозиторий
- API-ключи Bybit: давать только права `Contract → Read & Write`
- Рекомендуется привязать IP сервера в настройках ключа на Bybit

## Риски

> ⚠️ Торговля фьючерсами с плечом несёт высокий риск потери капитала.  
> ⚠️ Мемкоины крайне волатильны — возможны flash-crash на 30–50% за часы.  
> ⚠️ Бот не гарантирует прибыль. Используй только те деньги, которые готов потерять.

## Roadmap

- [ ] Hermes LLM-агент для управления ботом через естественный язык
- [ ] Docker + docker-compose деплой
- [ ] Web-дашборд с графиками PnL
- [ ] Поддержка нескольких аккаунтов

## Лицензия

MIT — используй свободно, но на свой риск.
