SHELL := /bin/bash
PY := .venv/bin/python
export DAGSTER_HOME := $(CURDIR)/.dagster
export PYDANTIC_AI_NO_BANNER := 1

.PHONY: help install up up-catalog down seed build dagster app mcp record reset-seventh demo test clean

help:            ## list the targets
	@grep -E '^[a-z-]+:.*##' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  %-15s %s\n", $$1, $$2}'

install:         ## create the venv and install the project
	uv venv -q --python 3.12 && uv pip install -q --python $(PY) -e ".[dev]"
	@mkdir -p $(DAGSTER_HOME)

up:              ## start Postgres, Neo4j and Qdrant
	docker compose up -d postgres neo4j qdrant

up-catalog:      ## also start OpenMetadata (needs ~8 GB Docker memory)
	docker compose --profile catalog up -d

down:            ## stop every container
	docker compose --profile catalog down

seed:            ## generate the insurer's source systems (Postgres, CSVs, PDFs)
	$(PY) -m claims.synth.generate

build:           ## build all six layers through Dagster, headless
	.venv/bin/dagster asset materialize --select '*' -m claims.pipeline.definitions

dagster:         ## Dagster UI on :3010 (the layers, their lineage and checks)
	.venv/bin/dagster dev -p 3010 -m claims.pipeline.definitions

app:             ## the demo app on :8010
	.venv/bin/uvicorn claims.app.server:app --port 8010

mcp:             ## the discovery plane as an MCP server on :8020
	$(PY) -m claims.mcp_server --http

record:          ## re-record the agent at every stage (fallback for flaky Wi-Fi)
	$(PY) -m claims.record

reset-seventh:   ## take the seventh claim out again, to rehearse AI for Data
	$(PY) -c "from claims import ai_for_data; print(ai_for_data.reset())"

demo: up seed build record  ## everything from a clean machine, except the UIs
	@echo "Now run: make dagster   and   make app"

test:            ## the test suite
	.venv/bin/pytest -q

clean:           ## remove generated data (keeps recorded runs)
	rm -rf data/lakehouse data/sources data/generated data/audit
