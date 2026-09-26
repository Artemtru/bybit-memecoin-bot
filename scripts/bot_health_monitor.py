#!/usr/bin/env python3
"""
Мониторинг здоровья Bybit бота на VPS
Проверяет статус systemd сервисов и последние данные из status.json / trades.json
"""
import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path


def run_cmd(cmd):
    """Запускает команду и возвращает stdout"""
    try:
        result = subprocess.run(
            cmd, shell=True, capture_output=True, text=True, timeout=10
        )
        return result.stdout.strip(), result.returncode
    except Exception as e:
        return f"ERROR: {e}", 1


def check_systemd_service(service_name):
    """Проверяет статус systemd сервиса"""
    output, code = run_cmd(f"systemctl is-active {service_name}")
    is_active = output == "active"
    
    # Получаем uptime
    uptime_out, _ = run_cmd(
        f"systemctl show {service_name} --property=ActiveEnterTimestamp --value"
    )
    
    return {
        "service": service_name,
        "status": "🟢 Running" if is_active else "🔴 Stopped",
        "active": is_active,
        "uptime": uptime_out or "unknown",
        "exit_code": code
    }


def read_json_file(path):
    """Читает JSON файл"""
    try:
        with open(path, 'r') as f:
            return json.load(f)
    except FileNotFoundError:
        return {"error": "file_not_found"}
    except json.JSONDecodeError:
        return {"error": "invalid_json"}
    except Exception as e:
        return {"error": str(e)}


def format_health_report(data):
    """Форматирует отчет для Telegram"""
    lines = ["🤖 **Bybit Bot Health Check**", ""]
    
    # Systemd services
    lines.append("**📊 Services Status:**")
    for svc in data.get("services", []):
        lines.append(f"• {svc['service']}: {svc['status']}")
        if svc.get('uptime') and svc['uptime'] != 'unknown':
            lines.append(f"  Uptime: {svc['uptime']}")
    lines.append("")
    
    # Bot status
    status = data.get("bot_status", {})
    if "error" not in status:
        lines.append("**💰 Bot Status:**")
        lines.append(f"• Balance: ${status.get('balance', 'N/A')}")
        lines.append(f"• Daily PnL: ${status.get('daily_pnl', 'N/A')}")
        lines.append(f"• Active positions: {status.get('active_positions', 0)}")
        lines.append(f"• Last update: {status.get('timestamp', 'N/A')}")
    else:
        lines.append(f"**⚠️ Bot Status:** {status['error']}")
    lines.append("")
    
    # Recent trades
    trades = data.get("recent_trades", [])
    if trades and not isinstance(trades, dict):
        lines.append(f"**📈 Recent Trades:** {len(trades)} in history")
        for trade in trades[-3:]:  # Last 3 trades
            symbol = trade.get('symbol', 'N/A')
            side = trade.get('side', 'N/A')
            pnl = trade.get('pnl', 0)
            pnl_emoji = "✅" if pnl > 0 else "❌"
            lines.append(f"  {pnl_emoji} {symbol} {side}: ${pnl:.2f}")
    else:
        lines.append("**📈 Trades:** No data")
    
    lines.append("")
    lines.append(f"⏰ Checked: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    
    return "\n".join(lines)


def main():
    """Собирает все данные и выводит отчет"""
    bot_dir = Path("/root/bybit-memecoin-bot")
    
    health_data = {
        "timestamp": datetime.now().isoformat(),
        "services": [
            check_systemd_service("memecoin-bot.service"),
            check_systemd_service("telegram-bot.service")
        ],
        "bot_status": read_json_file(bot_dir / "status.json"),
        "recent_trades": read_json_file(bot_dir / "trades.json")
    }
    
    # Форматируем и выводим отчет
    report = format_health_report(health_data)
    print(report)
    
    # Также выводим JSON для машинной обработки (в stderr)
    print(json.dumps(health_data, indent=2), file=sys.stderr)
    
    # Exit code зависит от статуса сервисов
    all_active = all(s["active"] for s in health_data["services"])
    return 0 if all_active else 1


if __name__ == "__main__":
    sys.exit(main())
