# Demo script

The demo follows the deck's claim, CLM-88421, through one question asked twice. The story
slides (`http://localhost:8010/static/story.html#<slide>`) frame each step; the real tools show
each layer; the app at `http://localhost:8010` is only the agentic UI (the agent run with its
scorecard, and a chat on the same data).

Before the talk: `make up up-catalog dagster app`, run `make reset-seventh`, and open these in
one browser window, logged in where needed (OpenMetadata admin@open-metadata.org / admin; Neo4j
neo4j / claims-graph). Dismiss Dagster's "Join the community" dialog once; it stays dismissed.

| # | Screen | What happens | Narration (`scripts/video/narration.json`) |
|---|---|---|---|
| 1 | Story · `#title` | The premise: same agent, same prompt, only the data changes | `title` |
| 2 | Story · `#claim` | The prompt, and what a good decision needs | `claim` |
| 3 | App · Assess · **Agent-first** · Run live | SQL over raw tables, live policy API, a classifier that never reads | `before_run` |
| 4 | App · result and scorecard | Fast-track, payout queued, 0 of 7 | `before_result` |
| 5 | Story · `#faults` | Six faults, six layers | `faults` |
| 6 | Story · `#layer-A` → Dagster lineage → `silver_policy_versions` | Policy history rebuilt: zero-dep added the day after the accident | `layer_a` |
| 7 | Story · `#layer-B` → OpenMetadata glossary *Severity Band* | One owned definition | `layer_b` |
| 8 | Story · `#layer-C` → Qdrant `documents` | Passages with document, page, section, PII tags | `layer_c` |
| 9 | Story · `#layer-D` → OpenMetadata `silver.customers` | PII classified; only investigators may see it | `layer_d` |
| 10 | Story · `#layer-E` → Neo4j queries 1 and 5 | The ring; where the phone lives and who may see it | `layer_e` |
| 11 | Story · `#layer-F` → Dagster `claim_signals` | fraud_score 0.82, versioned; the live API says 0.02 | `layer_f` |
| 12 | Story · `#payoff` → App · Assess · **F** · Run live | Same prompt, six layers | `after_run` |
| 13 | App · result and scorecard | Held and routed with evidence, 7 of 7 | `after_result` |
| 14 | App · All stages | The matrix: one layer at a time | `matrix` |
| 15 | Story · `#ai4data` → Dagster job `seventh_claim` → Launch | Ingest, extract (model), propose, validate (rules), alert | `ai4data` |
| 16 | Neo4j query 8 | The new links, proposed by the model, validated by rules; ring 6 → 7 | `ai4data_graph` |
| 17 | App · Ask the agent | The phone question as a claims officer: answered, masked | `chat` |
| 18 | Story · `#closing` | Before you make your AI smarter, make your data ready | `closing` |

The agent's instructions carry the insurer's business goal (straight-through processing: fast-track
by default, hold only on concrete evidence of risk). They are identical at every stage, so the
agent-first build pays the claim because it cannot see the evidence, and the full stack holds it
because it can.

## Rebuilding the narrated video

```bash
.venv/bin/python scripts/video/tts.py                       # narration clips (en-IN-NeerjaExpressiveNeural)
# record the walkthrough (Chrome screencast), noting when each segment starts in marks.json
.venv/bin/python scripts/video/assemble.py screencast.mp4 marks.json media/claims-ai-ready-demo.mp4
```

Timings with `gpt-5.6-luna`: 25–40 s per live run. If the network is slow, use **Replay
recorded** in the app; it shows the same trace and scorecard from the last good run.
