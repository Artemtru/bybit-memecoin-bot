# 🔧 Обновление MAX_BOTS=5 на VPS

## Проблема
Бот на VPS работает с `MAX_BOTS=3`, нужно изменить на `MAX_BOTS=5`.

## Решение (SSH на VPS)

### Вариант 1: Автоматический скрипт

```bash
ssh root@13.140.145.240 << 'EOF'
cd /root/bybit-memecoin-bot

# Backup текущего .env
cp .env .env.backup_$(date +%Y%m%d_%H%M%S)

# Обновить MAX_BOTS
sed -i 's/^MAX_BOTS=.*/MAX_BOTS=5/' .env

echo "✅ Updated:"
grep '^MAX_BOTS=' .env

# Перезапустить сервисы
systemctl restart memecoin-bot telegram-bot
sleep 3

# Проверить статус
systemctl is-active memecoin-bot && echo "✅ memecoin-bot: active" || echo "❌ memecoin-bot: FAILED"
systemctl is-active telegram-bot && echo "✅ telegram-bot: active" || echo "❌ telegram-bot: FAILED"
EOF
```

### Вариант 2: Вручную

```bash
# 1. SSH на VPS
ssh root@13.140.145.240

# 2. Перейти в директорию бота
cd /root/bybit-memecoin-bot

# 3. Бэкап .env
cp .env .env.backup_$(date +%Y%m%d_%H%M%S)

# 4. Обновить MAX_BOTS
sed -i 's/^MAX_BOTS=.*/MAX_BOTS=5/' .env

# 5. Проверить изменения
grep '^MAX_BOTS=' .env
# Должно показать: MAX_BOTS=5

# 6. Перезапустить сервисы
systemctl restart memecoin-bot telegram-bot

# 7. Проверить статус
systemctl status memecoin-bot telegram-bot
```

## Проверка результата

После обновления выполни в Telegram боте:

```
/status
```

**Ожидаемый результат:**
```
📊 Статус системы

Активные боты: X/5        ← было X/3
Монеты: DOGEUSDT, SHIBUSDT, PEPEUSDT, FLOKIUSDT, WIFUSDT
PnL за день: +0.0000 USDT
...
```

Команды `/top` и `/coins` теперь будут показывать до **5 монет** одновременно вместо 3.

## Дополнительное улучшение (опционально)

Если хочешь полную агрессивную конфигурацию, можно обновить все параметры сразу:

```bash
ssh root@13.140.145.240 << 'EOF'
cd /root/bybit-memecoin-bot
cp .env .env.backup_$(date +%Y%m%d_%H%M%S)

sed -i 's/^MAX_BOTS=.*/MAX_BOTS=5/' .env
sed -i 's/^QTY_USDT=.*/QTY_USDT=30/' .env
sed -i 's/^SCAN_INTERVAL_MIN=.*/SCAN_INTERVAL_MIN=15/' .env
sed -i 's/^RSI_BUY=.*/RSI_BUY=35/' .env
sed -i 's/^RSI_SELL=.*/RSI_SELL=65/' .env

echo "✅ Updated parameters:"
grep -E '^(MAX_BOTS|QTY_USDT|SCAN_INTERVAL_MIN|RSI_BUY|RSI_SELL)=' .env

systemctl restart memecoin-bot telegram-bot
EOF
```

**Это увеличит количество сделок** с 2/неделю до 5-10/день.

## Почему это решает проблему "не все комнаты"

- **До:** `scan(top_n=3)` — сканер выбирает только ТОП-3 монеты
- **После:** `scan(top_n=5)` — сканер выбирает ТОП-5 монет
- В команде `/coins` будет показываться **больше монет** (максимум 5 вместо 3)

**Важно:** Не все 12 мемкоинов будут показаны — только те, которые прошли фильтры:
- Объём 24h ≥ 250,000 USDT
- Изменение цены: 3-30%
- RSI: 20-80

Если рынок спокойный, может быть меньше 5 монет — это нормально.
