## 🔍 Анализ логов торговли

### ⚠️ **ВАЖНО: Логи недоступны из Docker-контейнера**

Бот работает на **хосте VPS** (`/root/bybit-memecoin-bot/`), а Hermes Agent находится в **Docker-контейнере** (`/opt/data/`) без доступа к хостовой файловой системе.

---

### 📂 **Где искать логи:**

```bash
# На хосте VPS (SSH: vmd198985 / 13.140.145.240):
cd /root/bybit-memecoin-bot/logs/

# Файлы логов:
ls -lh logs/
  ├── manager_YYYYMMDD.log     # основной лог торговли
  ├── trades.json               # JSON-база всех сделок
  ├── status.json               # текущий статус (читает Telegram-бот)
  ├── service.log               # stdout systemd
  └── service-error.log         # stderr systemd
```

---

### 🔎 **Как проверить логи вручную:**

#### 1. **Проверка последней торговой активности:**
```bash
# Последние 50 строк сегодняшнего лога
tail -50 /root/bybit-memecoin-bot/logs/manager_$(date +%Y%m%d).log

# Или самый свежий лог (если бот работал вчера):
ls -lt /root/bybit-memecoin-bot/logs/manager_*.log | head -1 | xargs tail -100
```

#### 2. **Анализ trades.json:**
```bash
# Проверить количество сделок
cat /root/bybit-memecoin-bot/logs/trades.json | grep '"id"' | wc -l

# Последние 10 сделок
cat /root/bybit-memecoin-bot/logs/trades.json | jq '.[-10:]'

# Сделки за последние 3 дня
cat /root/bybit-memecoin-bot/logs/trades.json | jq '[.[] | select(.date >= "2026-08-01")]'
```

#### 3. **Проверить статус systemd:**
```bash
sudo systemctl status popcat-bot.service telegram-bot.service
sudo journalctl -u popcat-bot.service --since "2 days ago" | tail -100
```

---

### 🚨 **Признаки того, что бот НЕ торгует:**

1. **Пустой trades.json** или отсутствует
2. **Логи manager_*.log** показывают только сканирование без записей об открытии позиций:
   ```
   [SYMBOL] Сигнала нет, ждём...
   [SYMBOL] Сигнала нет, ждём...
   ```
3. **Ошибки подключения к Bybit API:**
   ```
   Error: 10001 - Invalid API key
   Error: Network timeout
   ```
4. **Блокировка новых входов из-за просадки:**
   ```
   ⛔ Дневной лимит потерь!
   ⛔ Новые входы заблокированы адаптером
   ```

---

### 📊 **Ожидаемая структура trades.json:**

```json
[
  {
    "id": 1,
    "ts": "2026-08-01 10:23:15",
    "date": "2026-08-01",
    "week": "2026-W31",
    "month": "2026-08",
    "symbol": "DOGEUSDT",
    "side": "Buy",
    "result": "SL",
    "pnl": -0.85,
    "entry_price": 0.07234,
    "exit_price": 0.07164,
    "mode": "trend"
  },
  {
    "id": 2,
    ...
  }
]
```

---

### ✅ **Следующие шаги для диагностики:**

1. **SSH на VPS:**
   ```bash
   ssh root@13.140.145.240
   cd /root/bybit-memecoin-bot/
   ```

2. **Проверить структуру:**
   ```bash
   ls -la logs/
   cat logs/status.json
   tail -100 logs/manager_$(date +%Y%m%d).log
   ```

3. **Если trades.json пуст или отсутствует:**
   - Проверить ошибки в `logs/service-error.log`
   - Проверить статус: `systemctl status popcat-bot`
   - Проверить API-ключи в `.env`

4. **Если есть сделки, но всё убыточно:**
   - Применить текущий патч (см. CHANGELOG.md)
   - Перезапустить бота после обновления

---

### 🔗 **Автоматизация проверки:**

Можно создать cron-job для регулярной отправки статистики в Telegram:

```bash
# Добавить в crontab на хосте:
0 9 * * * cd /root/bybit-memecoin-bot && python3 -c "import analytics; print(analytics.format_report(analytics.today(), 'Сегодня'))"
```

---

**Вывод:** Без доступа к хосту невозможно проверить реальные логи торговли. Рекомендуется вручную проверить `/root/bybit-memecoin-bot/logs/` на VPS.
