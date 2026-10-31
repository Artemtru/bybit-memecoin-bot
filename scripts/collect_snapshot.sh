#!/bin/bash
# Печатает обезличенный снимок состояния бота. Запускается на VPS через
# `ssh root@VPS 'bash -s' < scripts/collect_snapshot.sh` из workflow snapshot.yml.
# Секреты НЕ печатаются: из .env выводится только белый список ключей.

cd /root/bybit-memecoin-bot || { echo "cannot cd to bot dir"; exit 1; }

echo "# Bot snapshot"
echo "generated_utc: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
echo "git_head: $(git rev-parse --short HEAD 2>/dev/null)"
echo
echo "## Services"
for s in memecoin-bot telegram-bot; do
  echo "$s: $(systemctl is-active $s 2>/dev/null)"
done
echo
echo "## Live .env (белый список, без секретов)"
grep -E '^(MAX_BOTS|QTY_USDT|MIN_POSITION_USDT|MAX_POSITION_USDT|LEVERAGE|INTERVAL|SCAN_INTERVAL_MIN|SLEEP_SEC|RSI_BUY|RSI_SELL|BB_PERIOD|BB_STD|ATR_PERIOD|ATR_TP_MULT|ATR_SL_MULT|SIDEWAYS_ATR_THRESHOLD|ADX_PERIOD|ADX_TREND_THRESHOLD|DAILY_LOSS_LIMIT|STOP_COOLDOWN_MIN|HERMES_ENABLED|MIN_SL_PCT|SIDEWAYS_RSI_LONG_MAX|SIDEWAYS_RSI_SHORT_MIN|ADAPT_RSI|ADAPTER_WRITE_ENV)=' .env 2>/dev/null || echo "(нет .env)"
echo
echo "## Trades (logs/trades.json)"
python3 - <<'PY'
import json, os
from collections import defaultdict

p = "logs/trades.json"
if not os.path.exists(p):
    print("no trades file")
    raise SystemExit
try:
    t = json.load(open(p, encoding="utf-8"))
except Exception as e:
    print("cannot parse trades.json:", e)
    raise SystemExit
n = len(t)
if n == 0:
    print("trades.json is empty")
    raise SystemExit

def pnl_of(x):
    return x.get("pnl") or 0

wins   = [x for x in t if pnl_of(x) > 0]
losses = [x for x in t if pnl_of(x) < 0]
total  = sum(pnl_of(x) for x in t)
gw = sum(pnl_of(x) for x in wins)
gl = -sum(pnl_of(x) for x in losses)
print(f"total={n} wins={len(wins)} losses={len(losses)} winrate={len(wins)/n*100:.1f}% pnl={total:+.4f}")
if wins:
    print(f"avg_win={gw/len(wins):.4f}")
if losses:
    print(f"avg_loss={-gl/len(losses):.4f}")
if gl:
    print(f"profit_factor={gw/gl:.2f}")

def group(key):
    d = defaultdict(lambda: [0, 0, 0.0])
    for x in t:
        k = str(x.get(key) or "?")
        d[k][0] += 1
        d[k][1] += 1 if pnl_of(x) > 0 else 0
        d[k][2] += pnl_of(x)
    return d

for key in ("mode", "symbol", "date"):
    print(f"\nby {key}:")
    items = sorted(group(key).items())
    if key == "date":
        items = items[-14:]
    for k, (c, w, pn) in items:
        print(f"  {k}: n={c} wr={w/c*100:.0f}% pnl={pn:+.4f}")

print("\nlast 15 trades:")
for x in t[-15:]:
    print(" ", json.dumps(x, ensure_ascii=False))
PY
echo
LOG=$(ls -t logs/manager_*.log 2>/dev/null | head -1)
if [ -n "$LOG" ]; then
  echo "## Входы (размер позиции — часть (~X\$), R:R — фактический)"
  echo "log_file: $LOG"
  grep -h "Limit IOC" "$LOG" | tail -10
  echo "entries_in_log: $(grep -c 'Limit IOC' "$LOG")"
  echo "trend_skips_in_log: $(grep -c 'Trending mode' "$LOG")"
  echo "cooldown_skips_in_log: $(grep -c 'Cooldown after stop' "$LOG")"
  echo
  echo "## Последние ошибки"
  grep -iE "error|traceback|exception" "$LOG" | tail -15
else
  echo "no manager log"
fi
echo
echo "## status.json"
head -c 3000 logs/status.json 2>/dev/null || echo "(нет status.json)"
echo
