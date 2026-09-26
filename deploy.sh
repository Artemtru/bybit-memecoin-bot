#!/bin/bash
#
# ╔════════════════════════════════════════════════════════════╗
# ║         DEPLOY.SH — Деплой AI бота на VPS                  ║
# ║                                                             ║
# ║  Использование:                                             ║
# ║    ssh root@VPS_IP "bash -s" < deploy.sh                   ║
# ║                                                             ║
# ║  Или на самом VPS:                                          ║
# ║    ./deploy.sh                                              ║
# ╚════════════════════════════════════════════════════════════╝

set -e  # прерваться при ошибке

BOT_DIR="/opt/data/bybit-memecoin-bot"
LOG_DIR="$BOT_DIR/logs"

echo "════════════════════════════════════════════════════════════"
echo "  🚀 Bybit Memecoin Bot — Деплой AI-версии (режим Б)"
echo "════════════════════════════════════════════════════════════"
echo ""

# ─────────────────────────────────────────────────────────────
#  1. Проверка директории
# ─────────────────────────────────────────────────────────────
if [ ! -d "$BOT_DIR" ]; then
    echo "❌ Директория $BOT_DIR не существует"
    echo "   Клонируйте репозиторий: git clone <repo_url> $BOT_DIR"
    exit 1
fi

cd "$BOT_DIR"
echo "✓ Рабочая директория: $(pwd)"

# ─────────────────────────────────────────────────────────────
#  2. Остановка старых процессов
# ─────────────────────────────────────────────────────────────
echo ""
echo "🛑 Останавливаю старые процессы..."

if pgrep -f "python.*manager.py" > /dev/null; then
    pkill -f "python.*manager.py" && echo "  ✓ manager.py остановлен"
else
    echo "  ℹ️ manager.py не запущен"
fi

if pgrep -f "python.*telegram_commands.py" > /dev/null; then
    pkill -f "python.*telegram_commands.py" && echo "  ✓ telegram_commands.py остановлен"
else
    echo "  ℹ️ telegram_commands.py не запущен"
fi

sleep 2

# ─────────────────────────────────────────────────────────────
#  3. Git pull
# ─────────────────────────────────────────────────────────────
echo ""
echo "📥 Обновляю код..."

git fetch origin
CURRENT_HASH=$(git rev-parse HEAD)
REMOTE_HASH=$(git rev-parse origin/main)

if [ "$CURRENT_HASH" = "$REMOTE_HASH" ]; then
    echo "  ℹ️ Код уже актуален (commit: ${CURRENT_HASH:0:7})"
else
    echo "  📥 Новые изменения обнаружены"
    echo "     Текущий: ${CURRENT_HASH:0:7}"
    echo "     Новый:   ${REMOTE_HASH:0:7}"
    git pull origin main
    echo "  ✓ Код обновлён"
fi

# ─────────────────────────────────────────────────────────────
#  4. Проверка зависимостей
# ─────────────────────────────────────────────────────────────
echo ""
echo "📦 Проверяю зависимости..."

if [ -f "requirements.txt" ]; then
    pip install -q --no-cache-dir -r requirements.txt
    echo "  ✓ Зависимости установлены"
else
    echo "  ⚠️ requirements.txt не найден — пропускаю"
fi

# ─────────────────────────────────────────────────────────────
#  5. Валидация конфигурации
# ─────────────────────────────────────────────────────────────
echo ""
echo "🔍 Проверяю конфигурацию..."

if [ ! -f ".env" ]; then
    echo "❌ Файл .env отсутствует!"
    echo "   Создайте его из шаблона: cp .env.production .env"
    exit 1
fi

if ! python config_validator.py .env; then
    echo ""
    echo "❌ Конфигурация содержит ошибки. Деплой прерван."
    exit 1
fi

echo "  ✓ Конфигурация валидна"

# ─────────────────────────────────────────────────────────────
#  6. Создание директорий
# ─────────────────────────────────────────────────────────────
echo ""
echo "📁 Проверяю директории..."

mkdir -p "$LOG_DIR"
echo "  ✓ $LOG_DIR создан"

# ─────────────────────────────────────────────────────────────
#  7. Запуск процессов
# ─────────────────────────────────────────────────────────────
echo ""
echo "▶️  Запускаю процессы..."

# Manager (главная торговля)
nohup python manager.py > "$LOG_DIR/manager.log" 2>&1 &
MANAGER_PID=$!
echo "  ✓ manager.py запущен (PID: $MANAGER_PID)"

# Telegram commands
nohup python telegram_commands.py > "$LOG_DIR/telegram.log" 2>&1 &
TELEGRAM_PID=$!
echo "  ✓ telegram_commands.py запущен (PID: $TELEGRAM_PID)"

sleep 3

# ─────────────────────────────────────────────────────────────
#  8. Проверка процессов
# ─────────────────────────────────────────────────────────────
echo ""
echo "🔎 Проверяю статус..."

if ps -p $MANAGER_PID > /dev/null; then
    echo "  ✅ manager.py работает (PID: $MANAGER_PID)"
else
    echo "  ❌ manager.py упал — проверьте $LOG_DIR/manager.log"
    exit 1
fi

if ps -p $TELEGRAM_PID > /dev/null; then
    echo "  ✅ telegram_commands.py работает (PID: $TELEGRAM_PID)"
else
    echo "  ⚠️ telegram_commands.py упал — проверьте $LOG_DIR/telegram.log"
fi

# ─────────────────────────────────────────────────────────────
#  9. Финальный статус
# ─────────────────────────────────────────────────────────────
echo ""
echo "════════════════════════════════════════════════════════════"
echo "  ✅ Деплой завершён успешно!"
echo "════════════════════════════════════════════════════════════"
echo ""
echo "📊 Статус:"
echo "   Manager PID:   $MANAGER_PID"
echo "   Telegram PID:  $TELEGRAM_PID"
echo ""
echo "📝 Логи:"
echo "   tail -f $LOG_DIR/manager.log"
echo "   tail -f $LOG_DIR/telegram.log"
echo ""
echo "🤖 Telegram команды:"
echo "   /status  — статус системы"
echo "   /ask <команда> — AI управление"
echo ""
echo "🛑 Остановка:"
echo "   kill $MANAGER_PID $TELEGRAM_PID"
echo "   или: pkill -f 'python.*manager.py|telegram_commands.py'"
echo ""
