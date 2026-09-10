# ambient-streamer — thin wrappers over scripts/ and docker compose.
#
# Nothing here implements policy. Every target shells out to the script that
# already owns the job, so a `make` target and a hand-typed command cannot
# drift apart.
#
#   make                        list every target
#   make CHANNEL=lofi channel-new
#   make CHANNEL=lofi channel-start
#
# Ports are fixed on the target host: backend 8090, Icecast 8081, RTMP 1935,
# HLS 8888, MediaMTX API 9997. Nothing here introduces another.

SHELL := /bin/bash
.SHELLFLAGS := -eu -o pipefail -c
.DEFAULT_GOAL := help

PROJECT ?= ambient
TAG     ?= dev
# composer and liquidsoap are built per channel, so `docker compose build` on
# the global stack does not cover them. Build all four here.
IMAGES  ?= backend composer liquidsoap mediamtx
CHANNEL ?=
PYTHON  ?= python3

COMPOSE := docker compose -p $(PROJECT)

.PHONY: help install build up down test lint compile fallback verify capacity \
        channel-new channel-start channel-stop clean require-channel

help: ## List every target (default)
	@printf 'ambient-streamer %s — make targets\n\n' "$$(cat VERSION)"
	@awk 'BEGIN { FS = ":.*?## " } /^[a-zA-Z0-9_-]+:.*?## / { printf "  %-14s %s\n", $$1, $$2 }' $(MAKEFILE_LIST)
	@printf '\nVariables: CHANNEL=<name>  PROJECT=%s  TAG=%s  PYTHON=%s\n' '$(PROJECT)' '$(TAG)' '$(PYTHON)'

install: ## First-run host setup — config, secrets, port and encoder probes
	scripts/install.sh

build: ## Build all four images (backend, composer, liquidsoap, mediamtx)
	for image in $(IMAGES); do \
		echo "==> ambient-$$image:$(TAG)"; \
		docker build -f "docker/Dockerfile.$$image" -t "ambient-$$image:$(TAG)" .; \
	done
	docker images --filter 'reference=ambient-*:$(TAG)' --format 'table {{.Repository}}\t{{.Tag}}\t{{.Size}}'

up: ## Start the global stack (backend, icecast, mediamtx)
	$(COMPOSE) up -d
	$(COMPOSE) ps

down: ## Stop the global stack — per-channel projects are separate and survive
	$(COMPOSE) down

test: ## Run the backend test suite
	pytest backend -q

lint: ## ruff on backend, bash -n and shellcheck on every script
	@if command -v ruff >/dev/null 2>&1; then \
		ruff check --target-version py310 --select E4,E7,E9,F backend; \
	else \
		echo "ruff not installed — skipped here, CI still runs it"; \
	fi
	@shopt -s globstar nullglob; \
	for f in **/*.sh; do \
		case "$$f" in spikes/*) continue ;; esac; \
		bash -n "$$f" && echo "ok   $$f"; \
	done
	@if command -v shellcheck >/dev/null 2>&1; then \
		shopt -s globstar nullglob; \
		files=(); \
		for f in **/*.sh; do case "$$f" in spikes/*) continue ;; esac; files+=("$$f"); done; \
		shellcheck -S error "$${files[@]}" && echo "shellcheck clean"; \
	else \
		echo "shellcheck not installed — skipped here, CI still runs it"; \
	fi

compile: ## Compile config -> playlist.m3u, images.list, compose file (CHANNEL=, default all)
	PYTHONPATH=backend $(PYTHON) -m ambient.compile $(if $(CHANNEL),$(CHANNEL),--all)

fallback: ## Encode an Icecast fallback MP3, 256k/44.1k/stereo (CHANNEL=, default 'default')
	scripts/make-fallback.sh $(if $(CHANNEL),$(CHANNEL),default)

verify: ## Bring the stack up, prove Icecast serves its fallback, tear it down
	scripts/verify-stack.sh

capacity: ## Report core, channel, uplink and NVENC headroom on this host
	scripts/capacity-check.sh

channel-new: require-channel ## Scaffold a channel (CHANNEL=<name>)
	scripts/new-channel.sh "$(CHANNEL)"

channel-start: require-channel ## Start one channel's two containers (CHANNEL=<name>)
	scripts/channel.sh start "$(CHANNEL)"

channel-stop: require-channel ## Stop one channel's two containers (CHANNEL=<name>)
	scripts/channel.sh stop "$(CHANNEL)"

clean: ## Delete generated artifacts — never media, .env, config.yaml or images
	rm -f channels/*/docker-compose.yml channels/*/playlist.m3u channels/*/images.list
	rm -rf backend/.pytest_cache backend/ambient_backend.egg-info
	find . -name '__pycache__' -type d -prune -exec rm -rf {} +
	@echo "generated artifacts removed; re-create them with: make compile"

require-channel:
	@if [[ -z "$(CHANNEL)" ]]; then \
		echo "set CHANNEL=<name>, for example: make CHANNEL=lofi $(MAKECMDGOALS)" >&2; \
		exit 1; \
	fi
