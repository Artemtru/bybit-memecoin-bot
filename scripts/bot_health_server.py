#!/usr/bin/env python3
"""
Простой HTTP сервер для мониторинга бота
Запускается на VPS на порту 9876, возвращает статус бота в JSON
"""
from http.server import HTTPServer, BaseHTTPRequestHandler
import json
import subprocess
from pathlib import Path
from datetime import datetime


class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        """Обрабатывает GET /health"""
        if self.path != "/health":
            self.send_error(404)
            return
        
        try:
            health_data = self.collect_health()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(health_data, indent=2).encode())
        except Exception as e:
            self.send_error(500, str(e))
    
    def collect_health(self):
        """Собирает данные о здоровье бота"""
        bot_dir = Path("/root/bybit-memecoin-bot")
        
        # Systemd services
        services = []
        for svc in ["memecoin-bot.service", "telegram-bot.service"]:
            try:
                result = subprocess.run(
                    ["systemctl", "is-active", svc],
                    capture_output=True, text=True, timeout=5
                )
                is_active = result.stdout.strip() == "active"
                services.append({"name": svc, "active": is_active})
            except Exception as e:
                services.append({"name": svc, "active": False, "error": str(e)})
        
        # Bot files
        status_data = {}
        trades_data = []
        
        try:
            if (bot_dir / "status.json").exists():
                with open(bot_dir / "status.json") as f:
                    status_data = json.load(f)
        except Exception as e:
            status_data = {"error": str(e)}
        
        try:
            if (bot_dir / "trades.json").exists():
                with open(bot_dir / "trades.json") as f:
                    all_trades = json.load(f)
                    trades_data = all_trades[-5:]  # Last 5 trades
        except Exception as e:
            trades_data = {"error": str(e)}
        
        return {
            "timestamp": datetime.now().isoformat(),
            "services": services,
            "bot_status": status_data,
            "recent_trades": trades_data
        }
    
    def log_message(self, format, *args):
        """Подавляем логи запросов"""
        pass


if __name__ == "__main__":
    PORT = 9876
    server = HTTPServer(("0.0.0.0", PORT), HealthHandler)
    print(f"✓ Health monitor listening on port {PORT}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n✓ Shutdown")
