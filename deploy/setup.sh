#!/bin/bash
# ╔══════════════════════════════════════════════╗
# ║     setup.sh — Автоматический деплой        ║
# ║  Запускать от имени своего пользователя      ║
# ║  sudo bash deploy/setup.sh                  ║
# ╚══════════════════════════════════════════════╝

set -e

USER_NAME=$(logname 2>/dev/null || echo $SUDO_USER)
BOT_DIR="/home/$USER_NAME/bybit-memecoin-bot"
SERVICE_DIR="/etc/systemd/system"

echo "▶ Деплой в: $BOT_DIR"
echo "▶ Пользователь: $USER_NAME"

# 1. Зависимости
apt-get update -qq
apt-get install -y python3 python3-pip python3-venv git

# 2. Виртуальное окружение
cd $BOT_DIR
sudo -u $USER_NAME python3 -m venv venv
sudo -u $USER_NAME venv/bin/pip install -r requirements.txt

# 3. Папка логов
sudo -u $USER_NAME mkdir -p logs

# 4. Установить .env если нет
if [ ! -f .env ]; then
    sudo -u $USER_NAME cp .env.example .env
    echo "⚠️  Создан .env из шаблона — заполни API-ключи: nano $BOT_DIR/.env"
fi

# 5. Systemd сервисы
for SERVICE in memecoin-bot telegram-bot; do
    sed "s/YOUR_USER/$USER_NAME/g; s|bybit-memecoin-bot|$BOT_DIR|g" \
        deploy/$SERVICE.service \
        > $SERVICE_DIR/$SERVICE.service
    echo "✅ Установлен сервис: $SERVICE"
done

# 6. Запуск
systemctl daemon-reload
systemctl enable memecoin-bot telegram-bot
systemctl start memecoin-bot telegram-bot

echo ""
echo "✅ Деплой завершён!"
echo "   Статус:  sudo systemctl status memecoin-bot"
echo "   Логи:    tail -f $BOT_DIR/logs/service-error.log"
echo "   Telegram: /status"
