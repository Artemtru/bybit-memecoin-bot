# ── Makefile — быстрые команды ───────────────────

.PHONY: install start stop restart logs status

install:
	python3 -m venv venv
	venv/bin/pip install -r requirements.txt
	cp -n .env.example .env || true
	mkdir -p logs
	@echo "✅ Готово. Заполни .env и запускай: make start"

start:
	sudo systemctl start memecoin-bot telegram-bot

stop:
	sudo systemctl stop memecoin-bot telegram-bot

restart:
	sudo systemctl restart memecoin-bot telegram-bot

status:
	sudo systemctl status memecoin-bot telegram-bot

logs:
	tail -f logs/service-error.log

deploy:
	sudo systemctl daemon-reload
	sudo systemctl enable memecoin-bot telegram-bot
	sudo systemctl start memecoin-bot telegram-bot
	@echo "✅ Оба сервиса запущены и добавлены в автозапуск"
