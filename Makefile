.PHONY: up down logs dev-up dev test lint demo reset scoreboard

# Local stack = docker-compose.local.yml + .env.local; dev server = docker-compose.dev.yml + .env.dev.
# A missing env file is copied from its example (empty keys still start the stack, on rules).
COMPOSE_LOCAL = docker compose -f docker-compose.local.yml --env-file .env.local
COMPOSE_DEV = docker compose -f docker-compose.dev.yml --env-file .env.dev

.env.local:
	cp .env.local.example .env.local

.env.dev:
	cp .env.dev.example .env.dev

up: .env.local
	$(COMPOSE_LOCAL) up --build

down: .env.local
	$(COMPOSE_LOCAL) down -v

logs: .env.local
	$(COMPOSE_LOCAL) logs -f runtime sim

dev-up: .env.dev
	$(COMPOSE_DEV) up -d --build

dev:
	./scripts/dev.sh

demo: up

test:
	PYTHONPATH=. python3 -m unittest discover -s tests -v

scoreboard:
	PYTHONPATH=. python3 tests/test_two_worlds.py

lint:
	ruff check backend drone shared sim tests scripts

reset:
	rm -f ledger.jsonl
