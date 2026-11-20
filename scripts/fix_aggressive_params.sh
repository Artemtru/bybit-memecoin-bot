#!/bin/bash
# ═══════════════════════════════════════════════════════════
# 🔧 Фикс параметров агрессивной стратегии
# ═══════════════════════════════════════════════════════════
# Применяет параметры из коммита 0e6eef1 к .env на VPS
# Запуск: bash scripts/fix_aggressive_params.sh
# ═══════════════════════════════════════════════════════════

set -e  # выход при ошибке

cd "$(dirname "$0")/.." || exit 1

echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "📋 Текущие параметры в .env:"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
grep -E "^(QTY_USDT|RSI_BUY|RSI_SELL|ATR_TP_MULT|ATR_SL_MULT|LEVERAGE)=" .env 2>/dev/null || echo "(параметры не найдены в .env)"

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "🔄 Обновление параметров..."
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

# Backup
cp .env .env.backup.$(date +%Y%m%d_%H%M%S)
echo "✅ Backup создан: .env.backup.$(date +%Y%m%d_%H%M%S)"

# Удаляем старые значения
sed -i.tmp '/^QTY_USDT=/d' .env
sed -i.tmp '/^RSI_BUY=/d' .env
sed -i.tmp '/^RSI_SELL=/d' .env
sed -i.tmp '/^ATR_TP_MULT=/d' .env
sed -i.tmp '/^ATR_SL_MULT=/d' .env
sed -i.tmp '/^LEVERAGE=/d' .env
rm -f .env.tmp

# Добавляем новые
cat >> .env << 'ENVEOF'

# ═══════════════════════════════════════════════════════════
# Агрессивная стратегия (обновлено 2026-09-08)
# Соответствует коммиту 0e6eef1
# ═══════════════════════════════════════════════════════════
LEVERAGE=2
QTY_USDT=50
RSI_BUY=40
RSI_SELL=60
ATR_TP_MULT=2.5
ATR_SL_MULT=1.2
ENVEOF

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "📋 Новые параметры:"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
grep -E "^(QTY_USDT|RSI_BUY|RSI_SELL|ATR_TP_MULT|ATR_SL_MULT|LEVERAGE)=" .env

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "🔄 Перезапуск ботов..."
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
sudo systemctl restart memecoin-bot telegram-bot

echo "⏳ Ожидание 5 секунд..."
sleep 5

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "📊 Статус сервисов:"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
sudo systemctl status memecoin-bot --no-pager -l | head -15
echo ""
sudo systemctl status telegram-bot --no-pager -l | head -15

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "📋 Последние строки лога:"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
tail -20 logs/manager_$(date +%Y%m%d).log 2>/dev/null || echo "Лог за сегодня не найден"

echo ""
echo "═══════════════════════════════════════════════════════════"
echo "✅ Готово!"
echo "═══════════════════════════════════════════════════════════"
echo ""
echo "Проверьте бота в Telegram через 1 минуту:"
echo "  /top — должно быть 5-10 монет"
echo "  /status — проверить RSI пороги (должны быть BUY <40 / SELL >60)"
echo ""
echo "Ожидаемый результат:"
echo "  • Сейчас: 1-2 сделки в течение 24 часов"
echo "  • Через неделю: 15-25 сделок, PnL ±10-20$"
echo ""
