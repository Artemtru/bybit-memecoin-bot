# 🔄 Миграция: popcat-bot → memecoin-bot

## Для кого эта инструкция

Если у вас уже установлен бот с сервисом **`popcat-bot.service`**, следуйте этой инструкции для переименования в **`memecoin-bot.service`**.

**Новые установки** автоматически используют правильное имя через `deploy/setup.sh`.

---

## ⚡ Быстрая миграция (5 минут)

```bash
# 1. SSH на VPS
ssh root@YOUR_VPS_IP

# 2. Перейти в директорию бота
cd /root/bybit-memecoin-bot  # или /home/YOUR_USER/bybit-memecoin-bot

# 3. Остановить старый сервис
sudo systemctl stop popcat-bot telegram-bot

# 4. Переименовать сервис
sudo mv /etc/systemd/system/popcat-bot.service \
        /etc/systemd/system/memecoin-bot.service

# 5. Обновить код из GitHub
git pull origin main

# 6. Перезагрузить systemd + запустить
sudo systemctl daemon-reload
sudo systemctl enable memecoin-bot telegram-bot
sudo systemctl start memecoin-bot telegram-bot

# 7. Проверить статус
sudo systemctl status memecoin-bot telegram-bot
```

---

## ✅ Проверка

После миграции проверьте:

```bash
# Статус сервисов
systemctl list-units | grep bot
# Должен показать:
#   memecoin-bot.service   loaded active running
#   telegram-bot.service   loaded active running

# Логи
tail -20 logs/manager_$(date +%Y%m%d).log

# Telegram
# Отправить боту: /status
```

---

## 🔙 Откат (если что-то пошло не так)

```bash
# Вернуть старое имя
sudo systemctl stop memecoin-bot telegram-bot
sudo mv /etc/systemd/system/memecoin-bot.service \
        /etc/systemd/system/popcat-bot.service
sudo systemctl daemon-reload
sudo systemctl start popcat-bot telegram-bot
```

---

## 📝 Что изменилось

| Было | Стало |
|------|-------|
| `popcat-bot.service` | `memecoin-bot.service` |
| `systemctl status popcat-bot` | `systemctl status memecoin-bot` |
| `deploy/popcat-bot.service` | `deploy/memecoin-bot.service` |

**Функциональность не изменилась** — переименование только для консистентности с названием репозитория.

---

## ❓ FAQ

**Q: Почему вообще переименование?**  
A: Бот давно торгует не только POPCAT, а все мемкоины. Старое имя сбивало с толку.

**Q: Потеряются ли логи/данные?**  
A: Нет, все логи остаются в `logs/` директории. Сервис просто меняет имя в systemd.

**Q: Нужно ли менять что-то в .env?**  
A: Нет, `.env` не меняется.

**Q: Можно ли оставить старое имя?**  
A: Технически да, но лучше мигрировать для консистентности с документацией.

---

**Дата обновления:** 2026-08-04  
**Версия:** v2.0
