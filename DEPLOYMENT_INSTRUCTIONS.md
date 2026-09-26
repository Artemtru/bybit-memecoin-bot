# ⚙️ Обновление .env на VPS для агрессивного scalping

Подключись к VPS и выполни:

```bash
cd /root/bybit-memecoin-bot

# Бэкап текущего .env
cp .env .env.backup

# Обнови параметры
cat >> .env << 'EOF'

# ════ AGGRESSIVE SCALPING (Updated 2026-09-26) ════
QTY_USDT=30                    # было 20 → увеличено
SCAN_INTERVAL_MIN=15           # было 30 → ускорено
MAX_BOTS=5                     # было 3 → больше ботов
RSI_BUY=35                     # было 45 → раньше входим
RSI_SELL=65                    # было 55 → позже выходим
MAX_POSITION_USDT=50           # было 30 → AI может рекомендовать до 50
EOF

# Перезапусти сервисы
systemctl restart memecoin-bot telegram-bot

# Проверь логи
journalctl -u memecoin-bot -f
```

## После деплоя

AI Strategy Manager автоматически начнёт корректировать через **12 часов** (первый вызов).

Проверить статус AI:
```bash
cat /root/bybit-memecoin-bot/logs/ai_strategy_cache.json
```

Telegram уведомления покажут:
```
🧠 AI Strategy Adjustment
Market is volatile, widening RSI range
RSI: 30/70
Suggested QTY: 35 USDT
```

## Откат к старым параметрам

Если слишком агрессивно:
```bash
cd /root/bybit-memecoin-bot
cp .env.backup .env
systemctl restart memecoin-bot telegram-bot
```
