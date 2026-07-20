COMPOSE := docker compose
PYTHON := python3
VENV := .venv
TMUX_SESSION := tg_anonymous_chat_bot

.PHONY: help env venv install \
        build up down restart logs sh \
        tmux-start tmux-attach tmux-stop tmux-status tmux-logs \
        test lint clean

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*## ' $(MAKEFILE_LIST) | sed 's/:.*## /\t/'

env: ## Create .env from .env.example if it doesn't exist yet
	@[ -f .env ] || cp .env.example .env

## ---- Docker Compose (VPS / local dev) ----

build: env ## Build the bot image
	$(COMPOSE) build

up: env ## Start the bot in the background
	$(COMPOSE) up -d

down: ## Stop and remove the container
	$(COMPOSE) down

restart: ## Restart the bot container
	$(COMPOSE) restart

logs: ## Follow the bot container's logs
	$(COMPOSE) logs -f

sh: ## Open a shell inside the running container
	$(COMPOSE) exec bot bash

## ---- tmux (cPanel / any host without Docker) ----

venv: ## Create a local virtualenv in .venv (auto-recovers if ensurepip is broken, common on shared hosting)
	@if [ ! -x $(VENV)/bin/python ]; then \
		$(PYTHON) -m venv $(VENV) || $(PYTHON) -m venv $(VENV) --without-pip; \
	fi
	@if ! $(VENV)/bin/python -m pip --version >/dev/null 2>&1; then \
		echo "pip is missing from the venv (broken ensurepip) - bootstrapping it manually..."; \
		if command -v curl >/dev/null 2>&1; then \
			curl -sS https://bootstrap.pypa.io/get-pip.py -o /tmp/tg-anon-bot-get-pip.py; \
		elif command -v wget >/dev/null 2>&1; then \
			wget -q https://bootstrap.pypa.io/get-pip.py -O /tmp/tg-anon-bot-get-pip.py; \
		else \
			echo "neither curl nor wget found - falling back to Python's urllib"; \
			$(PYTHON) -c "import urllib.request; urllib.request.urlretrieve('https://bootstrap.pypa.io/get-pip.py', '/tmp/tg-anon-bot-get-pip.py')"; \
		fi && \
		$(VENV)/bin/python /tmp/tg-anon-bot-get-pip.py --quiet && \
		rm -f /tmp/tg-anon-bot-get-pip.py; \
	fi

install: venv env ## Install dependencies into .venv
	$(VENV)/bin/pip install --quiet --upgrade pip
	$(VENV)/bin/pip install -r requirements.txt

tmux-start: install ## Start the bot inside a persistent tmux session
	tmux new-session -d -s $(TMUX_SESSION) '$(VENV)/bin/python -m app.main 2>&1 | tee -a storage/logs/bot.log'
	@echo "Started in tmux session '$(TMUX_SESSION)'."
	@echo "Attach with: make tmux-attach   |   Stop with: make tmux-stop"

tmux-attach: ## Attach to the running tmux session (Ctrl+B then D to detach)
	tmux attach -t $(TMUX_SESSION)

tmux-stop: ## Stop the tmux session
	tmux kill-session -t $(TMUX_SESSION)

tmux-status: ## Check whether the tmux session is running
	@tmux list-sessions 2>/dev/null | grep $(TMUX_SESSION) || echo "Not running"

tmux-logs: ## Tail the bot's log file (written by tmux-start)
	tail -f storage/logs/bot.log

## ---- Dev ----

test: install ## Run the test suite
	$(VENV)/bin/pip install --quiet -r requirements-dev.txt
	$(VENV)/bin/pytest

lint: install ## Run ruff
	$(VENV)/bin/pip install --quiet -r requirements-dev.txt
	$(VENV)/bin/ruff check app tests

clean: ## Remove the virtualenv and Docker container/image
	rm -rf $(VENV)
	$(COMPOSE) down --rmi local 2>/dev/null || true
