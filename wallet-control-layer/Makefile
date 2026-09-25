.DEFAULT_GOAL := help
SHELL := /bin/bash
PY := uv run

help:  ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-14s\033[0m %s\n",$$1,$$2}'

setup:  ## Install everything (uv manages Python 3.11 — system 3.9 is not used)
	uv sync --extra dev
	@echo "✓ setup complete — next: make verify-data"

verify-data:  ## SHA-256 check the organizers' data pack against metadata.json
	$(PY) python tools/verify_data_pack.py

check: fmt lint typecheck test  ## Everything. Run before every handoff.

fmt:  ## Format
	$(PY) ruff format src sandbox payment_app tools tests
lint:  ## Lint
	$(PY) ruff check --fix src sandbox payment_app tools tests
typecheck:  ## Typecheck
	$(PY) mypy src
test:  ## Full test suite
	$(PY) pytest
vectors:  ## Language-neutral vector suite only
	$(PY) pytest tests/test_vectors.py -v

sandbox:  ## Offline replica of the organizers' API → http://127.0.0.1:8099
	$(PY) uvicorn sandbox.server:app --host 127.0.0.1 --port 8099 --reload

serve:  ## Our decision service against the OFFLINE replica → http://127.0.0.1:8000
	LEASH_BASE_URL=http://127.0.0.1:8099 $(PY) uvicorn leash.service.app:app \
		--host 127.0.0.1 --port 8000 --reload

live-pack:  ## Write the data pack the REAL sandbox serves to out/live-pack (every live target does this first)
	$(PY) python tools/live_pack.py

serve-live:  ## Our decision service against the REAL sandbox, on the pack it serves (needs .env with TEAM_API_KEY)
	$(PY) python tools/serve.py

probe-live:  ## Connectivity + contract probe against the REAL sandbox — run this first on the day
	$(PY) python tools/probe.py

payment-app:  ## The mock Payment App: OAuth server + the cardholder's phone → http://127.0.0.1:8081
	$(PY) uvicorn payment_app.server:app --host 127.0.0.1 --port 8081 --reload

connect:  ## Connector demo in ONE process: service + phone under /app → :8010 (needs make sandbox)
	$(PY) python tools/connect.py

probe-connector:  ## The official MCP client, end to end, against a running `make connect`
	uv run --with mcp python tools/connector_probe.py

audit:  ## Tail the append-only decision trail
	$(PY) python tools/audit.py

replay:  ## Run all 5 scenarios against the replica, print the 45-decision table
	$(PY) python tools/replay.py --all

replay-live:  ## Every scenario the REAL sandbox serves (needs TEAM_API_KEY; long — nothing answers its step-ups)
	$(PY) python tools/replay.py --all --live

demo:  ## The rehearsed demo sequence, fully offline
	$(PY) python tools/replay.py --demo

compile:  ## Compile the five pack instructions with the deterministic compiler
	LEASH_COMPILER=baseline $(PY) python tools/compile.py --scenarios

compile-llm:  ## Same five, model vs baseline side by side (needs OPENROUTER_API_KEY)
	$(PY) python tools/compile.py --scenarios --compare

replay-llm:  ## The 45-decision board, but every mandate compiled by the MODEL — must not move
	$(PY) python tools/replay.py --all --llm --diff-baseline

compile-live:  ## Live model check: compile the invented instructions for real (needs .env, costs money)
	LEASH_LIVE_COMPILER=1 $(PY) pytest tests/unit/test_invented_instructions.py -v

compile-invented:  ## Six instructions the compiler has never seen — generalisation check
	$(PY) python tools/compile.py --invented --compare

compare-providers:  ## Pack instructions: baseline vs OpenRouter vs Apertus — IR diffs + ms (needs both keys)
	$(PY) python tools/compare_providers.py

impact:  ## What the control layer adds over a plain spending limit, on the 45
	$(PY) python tools/impact.py

impact-detail:  ## Same, plus every attempt where the regimes disagree
	$(PY) python tools/impact.py --detail

tune:  ## Audit the concern weights — what fires, margins above threshold, dead config
	$(PY) python tools/tune.py

tune-sweep:  ## Sensitivity of the board to STEP_UP_THRESHOLD
	$(PY) python tools/tune.py --sweep

tune-weights:  ## Per-weight sensitivity — which knob owns which fixture
	$(PY) python tools/tune.py --weights

diff-decisions:  ## Diff the current decision table against the last saved baseline
	$(PY) python tools/replay.py --all --diff-baseline

baseline:  ## Save the current decision table as the regression baseline
	$(PY) python tools/replay.py --all --save-baseline

.PHONY: help setup verify-data check fmt lint typecheck test vectors sandbox serve serve-live \
	probe-live audit replay replay-live demo compile compile-llm compile-invented compare-providers \
	compile-live replay-llm payment-app connect probe-connector \
	tune tune-sweep tune-weights diff-decisions baseline impact impact-detail
