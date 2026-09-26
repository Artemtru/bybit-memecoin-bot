#!/bin/bash
# 🔧 Update MAX_BOTS on VPS
# Usage: ./update_max_bots.sh [value]
# Default: 5

set -e

MAX_BOTS="${1:-5}"
VPS_HOST="13.140.145.240"

echo "🔧 Updating MAX_BOTS to ${MAX_BOTS} on VPS..."
echo ""

ssh root@${VPS_HOST} << EOF
cd /root/bybit-memecoin-bot

# Backup current .env
cp .env .env.backup_\$(date +%Y%m%d_%H%M%S)
echo "✅ Backup created"

# Update MAX_BOTS
sed -i 's/^MAX_BOTS=.*/MAX_BOTS=${MAX_BOTS}/' .env
echo "✅ MAX_BOTS updated to ${MAX_BOTS}"

echo ""
echo "📝 Current configuration:"
grep -E '^(MAX_BOTS|QTY_USDT|SCAN_INTERVAL_MIN|RSI_BUY|RSI_SELL)=' .env

echo ""
echo "🔄 Restarting services..."
systemctl restart memecoin-bot telegram-bot
sleep 3

echo ""
echo "✅ Services status:"
systemctl is-active memecoin-bot && echo "  memecoin-bot: active" || echo "  memecoin-bot: FAILED"
systemctl is-active telegram-bot && echo "  telegram-bot: active" || echo "  telegram-bot: FAILED"

echo ""
echo "📊 Recent logs:"
tail -10 /root/bybit-memecoin-bot/logs/manager_\$(date +%Y%m%d).log 2>/dev/null || echo "  No logs yet"
EOF

echo ""
echo "🎉 Done!"
echo ""
echo "Теперь в Telegram выполни:"
echo "  /status  — увидишь 'Активные боты: X/5'"
echo "  /top     — покажет ТОП-5 монет"
echo "  /coins   — покажет до 5 монет одновременно"
