# Tech choices: what changed from the airline demo, and why

The airline demo (`airline-ops-intel`, March 2026) was the starting point. Most of its stack
still holds up; a few parts have aged badly in seven months, and one part (the "knowledge
graph") was the wrong design rather than the wrong tool. This is the review, layer by layer,
as of October 2026.

| Concern | Airline demo | This demo | Verdict |
|---|---|---|---|
| Object storage | MinIO | Local filesystem warehouse | **Replaced.** MinIO's community edition lost its console (May 2025), its binaries and images (Oct 2025), went to maintenance mode (Dec 2025) and was archived (Apr 2026). For a laptop demo nothing is needed under Iceberg; in production use S3/GCS/ADLS, or SeaweedFS / Garage if it must be self-hosted. |
| Table format | Apache Iceberg via PyIceberg (SQLite catalog) | Same (PyIceberg 0.12) | **Kept.** Iceberg is the format every engine now reads and writes. Production would put a REST catalog in front (Apache Polaris, Lakekeeper or Unity Catalog OSS). DuckLake is the interesting newcomer if you only ever use DuckDB. |
| Query engine | DuckDB | DuckDB 2.0 | **Kept.** Still the obvious in-process engine. It also hosts the governed, masked session the agent queries through. |
| Orchestration | Dagster | Dagster 1.13, one asset group per layer, asset checks as gates | **Kept, used better.** The asset graph *is* the "how each layer is built" view for the talk, and blocking asset checks are the medallion's quality gates. |
| Data quality | Great Expectations | Dagster asset checks | **Replaced for this demo.** GX is still widely downloaded, but its 1.x API churn and weight buy nothing here; gates that live on the asset, block downstream work and show in the same UI tell the story better. Soda Core (YAML checks) or dbt tests are the production choices if a separate tool is wanted. |
| Catalog | OpenMetadata 1.5 (pinned, MySQL + Elasticsearch) | OpenMetadata 1.13 (Postgres + Elasticsearch), fed from catalog-as-code | **Kept, upgraded.** 1.13 made MCP a first-class feature. Meaning lives in `catalog/*.yaml` (reviewable in Git) and is pushed to OpenMetadata, the lakehouse `meta.*` tables and the graph. DataHub is the main alternative. |
| Knowledge graph | Neo4j, holding airports/routes plus hand-typed governance nodes | Neo4j 5, a real enterprise knowledge graph (see below) | **Tool kept, design replaced.** Kùzu, the popular embedded option, was archived in Oct 2025. FalkorDB and Memgraph are credible alternatives; Neo4j's Browser is still the best thing to show on stage. |
| Vector store | Qdrant | Qdrant 1.15 | **Kept.** Payload filters carry the classifications retrieval enforces. pgvector is the alternative when you want one less service. |
| Embeddings | OpenAI `text-embedding-3-small` | Local `BAAI/bge-small-en-v1.5` via fastembed | **Replaced.** No key, no network, no cost for embeddings; one less thing to fail on conference Wi-Fi. |
| Agent framework | LangGraph `create_react_agent` + LangChain tools | PydanticAI 2.x with typed output | **Replaced.** The agent returns a typed `ClaimAssessment`, which is what makes the six-fault scorecard deterministic. Provider-neutral: Claude when an Anthropic key is present, OpenAI otherwise. LangGraph is still a fine choice for long-running multi-agent graphs; this demo needs neither. |
| Tool surface | Python functions inside the agent | A unified discovery plane, exposed in-process and as an MCP server | **Added.** The paper's System of Action is "tool calling across backends, governed through a gateway, often over MCP". The same governed functions serve the demo agent and any MCP client (Claude Desktop, Claude Code, MCP Inspector). |
| LLM | `gpt-4o-mini` | `gpt-5.4-mini` by default (or Claude); configurable | **Updated.** `gpt-5.5` is more thorough but takes ~45 s per run; the mini model takes 10–20 s, which suits a live talk. |
| Ingestion | pandas `read_sql` | psycopg → Arrow → Iceberg | **Same idea.** dlt is the production choice for incremental loads from many sources. |
| Mock APIs | FastAPI | FastAPI | **Kept.** |

## Running on Apple Silicon with Colima

Two things bit while building, and both are handled in `docker-compose.yml`:

- OpenSearch 2.x images refuse to start inside Colima/Lima VMs ("requires a newer ARM64
  processor"), so the catalog uses Elasticsearch 8.
- On M4 Macs the VM exposes SVE/SME, and JDK 21+ crashes with `SIGILL`. Every JVM in the
  catalog stack runs with `JAVA_TOOL_OPTIONS=-XX:UseSVE=0`.
- OpenMetadata needs room: give Colima 8 GB (`colima start --memory 8 --cpu 4`).

## Why the airline graph was not an enterprise knowledge graph

It held one source's domain rows (airports, routes, carriers) plus governance nodes typed by
hand, with no entity resolution, nothing extracted from documents and no provenance on any
edge. Nothing in it connected knowledge *across* sources, which is the whole point of the
layer. The graph here is built differently:

1. **Mentions** from every source: claims DB rows, tracker rows, CRM records, registry rows,
   and fields read from FIR and invoice pages (`silver.entity_mentions`).
2. **Resolution** of each mention by a named rule with a confidence: E.164 phone
   normalisation, GSTIN match, normalised name + fuzzy match, CRM identity resolution
   (`silver.entity_resolution`). "FixIt Motors", "Fixit Motor Works", "FIXIT MOTORS PVT.
   LTD." and "Fix-It Motors, Hadapsar" become one node.
3. **Relation with evidence**: every edge says which system or document page it came
   from, how it was made (record, template, model) and how sure it is. Claim-level links
   (`REPAIRED_AT`, `CONTACT_PHONE`) are derived from document mentions and cite them.
4. **Bridges to metadata and governance**: instances are `RECORDED_IN` datasets and
   `INSTANCE_OF` glossary terms; a phone is `STORED_IN` a column that is `CLASSIFIED_AS`
   `PII.Phone`, which is `VISIBLE_TO` investigators; source records `RESOLVES_TO` the person.

So one traversal answers "what links this claim to others, who says so, and who may see it"
— which is what `investigate_links` does, then pulls the cited passages from Qdrant by the
chunk IDs the graph holds (GraphRAG).
