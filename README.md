# AI-ready claims: one insurance claim, before and after AI-ready data

A working demo for the talk **AI-Ready Enterprise Data** (Vijayan and Kamthe, DevDay). It
follows the deck's claim, **CLM-88421**, a ₹4.2L car-collision claim, through an
agent-first build that gets it confidently wrong, and then through the six layers of
AI-ready data, one at a time, until the same agent with the same prompt holds the claim and
routes it to investigators.

The talk's claim becomes testable: **the answer changes because the data changed. The
model, the prompt and the instructions do not.**

| Stage | Policy date | "Severity" | FIR & invoice | Phone leak | Linked claims | Fraud score | Outcome |
|---|---|---|---|---|---|---|---|
| Agent-first | ✗ today's policy | ✗ | ✗ never read | ✗ leaked | ✗ missed | ✗ never called | ✗ queued for payout |
| … one layer at a time … | | | | | | | |
| A–F | ✓ as of 12 Oct | ✓ B2 = major repair | ✓ FIR p.2 cited | ✓ masked | ✓ 5 of 5 | ✓ 0.82, v3.2 | ✓ held, a person decides |

The scorecard is computed, not narrated: the agent returns a typed assessment and every
check reads that assessment and the tool trace (see `src/claims/agent.py`).

## What is in it

The insurer's source systems are synthetic but realistic, and every fault the deck names is
planted in them so the layers have something real to fix:

- **Claims DB** (Postgres): terse 2011-era columns, `dd/mm/yy` dates, three amounts in one
  row, a 1–5 legacy severity code.
- **Regional tracker** (CSV): the same claims as `CLM 88421` with ISO dates and Low/Med/High
  severity; two of the ring's claims exist only here.
- **Policy admin** (Postgres): current view only. The zero-depreciation add-on on P-2231 was
  endorsed on 13 Oct, the day *after* the accident; that history lives only in an audit log.
- **CRM** (Postgres): the claimant twice (4471 and 9032), plus a namesake who is not them.
- **Uploads** (PDFs): FIRs, invoices (one revised), a surveyor report. The repair shop and a
  shared contact phone appear only here, in four spellings and four phone formats, across
  six claims in thirty days.
- **Fraud engine** (API): recomputes a score from the raw row on every call; never sees the graph.

## The six layers, and where each one lives

| Layer | What it adds here | Tool you can show |
|---|---|---|
| A · Organization | One claim ID across systems, one person across CRM duplicates, policy versions rebuilt from the audit log, a document register with owners and versions, a quality gate that holds bad rows | Dagster (`layer_a_organization`), Iceberg tables |
| B · Semantics | Owned glossary (severity band, approved vs requested amount, fast-track rules), column meanings, AI-drafted meanings flagged for review, a discovery index | OpenMetadata glossary and tables, Dagster |
| C · Retrieval | Template parsing, section-aware chunks with a context header, local embeddings, citations by document, page and section | Qdrant dashboard (`documents` collection) |
| D · Governance | PII classified on columns and chunks, masking inside SQL and retrieval, an action gate (payments need a person), lineage, an audit log | OpenMetadata tags and lineage |
| E · Integration | An enterprise knowledge graph: mentions → resolution (E.164, GSTIN, fuzzy names, CRM identity) → evidence-carrying links, bridged to metadata and governance | Neo4j Browser (queries in `docs/neo4j-queries.cypher`) |
| F · Signals | Graph-aware features, fraud-model v3.2 scored once and versioned (0.82), a settlement benchmark (₹3.6L–₹4.4L) | Dagster (`claim_signals`), Iceberg |

The **discovery plane** (`src/claims/plane.py`) is the one surface the agent uses; each tool
belongs to the layer that makes it possible. The same governed functions are exposed as an
**MCP server** (`python -m claims.mcp_server`). **AI for Data** is the seventh claim: a
free-form FIR the template parser cannot read, read by a model, its links validated by rules
before they reach the graph (`src/claims/ai_for_data.py`).

See [docs/TECH_CHOICES.md](docs/TECH_CHOICES.md) for what changed from the airline demo and
why, and [docs/DEMO_SCRIPT.md](docs/DEMO_SCRIPT.md) for the run of show.

## Run it

Prerequisites: Docker (Colima is fine: `colima start --memory 8 --cpu 4`), Python 3.12,
[uv](https://docs.astral.sh/uv/), and an OpenAI or Anthropic API key in `.env`
(`OPENAI_API_KEY=…` or `ANTHROPIC_API_KEY=…`). Without a key the app replays recorded runs.

```bash
make install            # venv + dependencies
make up                 # Postgres 18, Neo4j 2026.10, Qdrant 1.20
make up-catalog         # optional: OpenMetadata 2.0 + Elasticsearch 9 (needs 8 GB Docker memory)
make seed               # generate the insurer's source systems
make build              # build all six layers through Dagster (≈30 s; syncs OpenMetadata if it is up)
make record             # run the agent at every stage and keep the runs for replay
make dagster            # Dagster UI      → http://localhost:3010
make app                # the demo app    → http://localhost:8010
```

| UI | URL | Login |
|---|---|---|
| Demo app (agentic UI) | http://localhost:8010 | — |
| Story slides | http://localhost:8010/static/story.html | — |
| Dagster | http://localhost:3010 | — |
| OpenMetadata | http://localhost:8585 | admin@open-metadata.org / admin |
| Neo4j Browser | http://localhost:7480 | neo4j / claims-graph (bolt://localhost:7690) |
| Qdrant | http://localhost:6340/dashboard | — |

The model defaults to `gpt-5.6-luna` (or Claude Sonnet 5.5 when an Anthropic key is set);
override with `CLAIMS_MODEL=openai:gpt-5.5` and similar. `make reset-seventh` takes the
seventh claim back out so AI for Data can be rehearsed.

All names, numbers and documents are synthetic and illustrative.
