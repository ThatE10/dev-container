# Dev Container — convenience wrapper around docker compose.
# Run `make` or `make help` to see everything.

# Use `docker compose` (v2) if available, else fall back to `docker-compose`.
COMPOSE ?= $(shell docker compose version >/dev/null 2>&1 && echo "docker compose" || echo "docker-compose")

.DEFAULT_GOAL := help

.PHONY: help up down restart shell logs code-logs status build rebuild pull new clean-context

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'
	@echo ""
	@echo "  First run: cp .env.example .env  (then fill it in)"
	@echo "  VS Code:   http://localhost:8443   after 'make up'"

up: ## Start the container in the background
	@test -f .env || { echo "No .env found — run: cp .env.example .env"; exit 1; }
	$(COMPOSE) up -d
	@echo ""
	@echo "  ✅ Up. VS Code → http://localhost:8443   SSH → ssh root@localhost -p 2222"

down: ## Stop and remove the container (volumes/context are kept)
	$(COMPOSE) down

restart: ## Restart the container
	$(COMPOSE) restart

shell: ## Open a bash shell inside the running container
	$(COMPOSE) exec dev bash

logs: ## Follow container logs
	$(COMPOSE) logs -f

code-logs: ## Tail the code-server log
	$(COMPOSE) exec dev tail -f /var/log/code-server.log

status: ## Show container + volume status
	$(COMPOSE) ps
	@echo ""
	@docker volume ls --filter name=dev-container

build: ## Build the image
	$(COMPOSE) build

rebuild: ## Rebuild from scratch (no cache) and restart — context volumes survive
	$(COMPOSE) build --no-cache
	$(COMPOSE) up -d

pull: ## Pull the prebuilt image from ghcr.io
	$(COMPOSE) pull

new: ## Scaffold a new project:  make new NAME=my-project
	@test -n "$(NAME)" || { echo "Usage: make new NAME=my-project"; exit 1; }
	@mkdir -p workspace/$(NAME)
	@$(COMPOSE) exec -T dev bash -lc 'cd /root/workspace/$(NAME) && \
		[ -d .git ] || git init -q && \
		[ -f .venv/bin/activate ] || python3 -m venv .venv && \
		echo ".venv/" > .gitignore' 2>/dev/null || true
	@echo "  ✅ workspace/$(NAME) ready (git + .venv). Open it in VS Code at http://localhost:8443"

clean-context: ## ⚠️  Delete persisted context volumes (extensions, pip --user, history, Claude config)
	@echo "This removes dev-local + claude-data volumes. Your ./workspace code is NOT touched."
	@read -p "Type 'yes' to continue: " ans && [ "$$ans" = "yes" ] || { echo "Aborted."; exit 1; }
	-$(COMPOSE) down
	-docker volume rm dev-container_dev-local dev-container_claude-data
