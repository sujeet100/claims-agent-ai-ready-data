"""The demo app: the agentic UI. The claims agent before and after each layer, and a chat on the same data.

    uvicorn claims.app.server:app --port 8010
"""

import threading
import uuid
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from claims import agent, config, governance, plane, sources

STATIC = Path(__file__).parent / "static"
app = FastAPI(title="AI-ready claims demo")
app.mount("/static", StaticFiles(directory=STATIC), name="static")

STAGE_COPY = {
    "before": {"title": "Agent-first build", "adds": "A SQL agent over the raw claims database, the live policy API, and a document tool that classifies but never reads.",
               "now": "query raw tables and call the policy API.", "not_yet": "everything the decision actually needs."},
    "A": {"title": "Enterprise Data Organization", "adds": "Standardise · Deduplicate · Keep history · Validate · Register documents",
          "now": "query one governed view of the claim, as of the accident date.", "not_yet": 'is "amount" requested, approved or paid? What does B2 mean?'},
    "B": {"title": "Metadata & Semantics", "adds": "Describe fields · Define terms, with owners · Map across systems",
          "now": "read each field by its owned definition.", "not_yet": "the FIR and invoice are still unreadable."},
    "C": {"title": "Retrieval-Ready Knowledge", "adds": "Extract · Chunk by section · Embed · Index with source",
          "now": "find passages by meaning, and cite them.", "not_yet": "it can surface details this user should not see."},
    "D": {"title": "Governance & Control", "adds": "Classify personal data · Trace lineage · Evaluate each request · Gate actions",
          "now": "act only within the rules, every use traced.", "not_yet": "each claim is still checked alone."},
    "E": {"title": "Knowledge Integration", "adds": "Identify entities · Resolve duplicates · Relate, with evidence",
          "now": "follow links from 88421 to related claims.", "not_yet": "every application still computes its own fraud score."},
    "F": {"title": "Insights & Signals", "adds": "Define once · Compute and store · Version and refresh · Serve",
          "now": "use prepared, versioned inputs, and still check live facts.", "not_yet": ""},
}

RUNS: dict[str, dict] = {}


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


@app.get("/api/meta")
def meta():
    return {"prompt": config.CASE_PROMPT, "claim": config.CASE_CLAIM, "model": config.default_model(),
            "llm_available": config.llm_available(), "dagster_url": "http://localhost:3010",
            "stages": [{"id": s, **STAGE_COPY[s], "tools": [{"name": fn.__name__, "backend": b} for fn, b in plane.available(s)]}
                       for s in plane.STAGES],
            "recorded": {s: agent.load(s) is not None for s in plane.STAGES}}


# --------------------------------------------------------------------------- agent runs

class RunRequest(BaseModel):
    stage: str = "F"
    role: str = "claims_officer"
    mode: str = "live"


@app.post("/api/run")
def start_run(req: RunRequest):
    if req.stage not in plane.STAGES:
        raise HTTPException(400, "unknown stage")
    if req.mode == "recorded":
        rec = agent.load(req.stage, req.role) or agent.load(req.stage)
        if not rec:
            raise HTTPException(404, "no recorded run for this stage yet; run it live once")
        rid = uuid.uuid4().hex[:8]
        RUNS[rid] = {"status": "done", "record": rec, "session": None, "replayed": True}
        return {"run_id": rid}
    rid = uuid.uuid4().hex[:8]
    session = plane.Session(stage=req.stage, principal=governance.Principal(role=req.role))
    job = {"status": "running", "record": None, "session": session, "error": None}
    RUNS[rid] = job

    def work():
        try:
            job["record"] = agent.run(req.stage, req.role, session=session)
            job["status"] = "done"
        except Exception as e:   # surface model or tool failures to the UI instead of hanging
            job["status"], job["error"] = "error", f"{type(e).__name__}: {e}"

    threading.Thread(target=work, daemon=True).start()
    return {"run_id": rid}


def _slim(trace: list[dict]) -> list[dict]:
    return [{**t, "output": t["output"][:400000]} for t in trace]   # the page parses outputs; never cut JSON mid-way


@app.get("/api/run/{rid}")
def run_status(rid: str):
    job = RUNS.get(rid)
    if not job:
        raise HTTPException(404)
    if job["status"] == "done":
        rec = dict(job["record"])
        rec["trace"] = _slim(rec["trace"])
        return {"status": "done", "record": rec, "replayed": job.get("replayed", False)}
    trace = job["session"].trace if job["session"] else []
    return {"status": job["status"], "trace": _slim(trace), "error": job.get("error")}


@app.get("/api/runs")
def recorded_runs(role: str = "claims_officer"):
    out = []
    for s in plane.STAGES:
        rec = agent.load(s, role)
        if rec:
            out.append({"stage": s, "decision": rec["assessment"]["decision"], "amount": rec["assessment"]["recommended_amount_inr"],
                        "scorecard": [{"fault": r["fault"], "pass": r["pass"], "detail": r["detail"]} for r in rec["scorecard"]],
                        "steps": len(rec["trace"]), "seconds": rec["seconds"], "model": rec["model"]})
    return out


# ------------------------------------------------------------------------------ chat

CHATS: dict[str, dict] = {}
SUGGESTIONS = [
    "Which claims are linked to FixIt Motors in the last 30 days, and what is the evidence?",
    "Was zero-depreciation cover in force on policy P-2231 on 12 October 2026?",
    "What does severity band B2 mean, and who owns that definition?",
    "Show me the FIR passage that names the repair shop for CLM-88421.",
    "What is the contact phone number on the FIR for CLM-77214?",
    "Where does gold.claim_signals come from?",
]


class ChatRequest(BaseModel):
    message: str
    conversation_id: str | None = None
    stage: str = "F"
    role: str = "claims_officer"


@app.get("/api/chat/suggestions")
def chat_suggestions():
    return SUGGESTIONS


@app.post("/api/chat")
def chat(req: ChatRequest):
    conv = CHATS.get(req.conversation_id or "")
    if not conv or conv["stage"] != req.stage or conv["role"] != req.role:   # a new stage or role starts a new conversation
        cid = uuid.uuid4().hex[:8]
        conv = CHATS[cid] = {"id": cid, "stage": req.stage, "role": req.role, "history": [], "turns": [],
                             "session": plane.Session(stage=req.stage, principal=governance.Principal(role=req.role))}
    turn = {"question": req.message, "answer": None, "status": "running", "error": None, "trace_from": len(conv["session"].trace)}
    conv["turns"].append(turn)

    def work():
        try:
            result = agent.chat_agent(req.stage).run_sync(req.message, deps=conv["session"], message_history=conv["history"])
            conv["history"] = result.all_messages()
            turn.update(answer=result.output, status="done")
        except Exception as e:
            turn.update(status="error", error=f"{type(e).__name__}: {e}")

    threading.Thread(target=work, daemon=True).start()
    return {"conversation_id": conv["id"], "turn": len(conv["turns"]) - 1}


@app.get("/api/chat/{cid}")
def chat_status(cid: str):
    conv = CHATS.get(cid)
    if not conv:
        raise HTTPException(404)
    trace = conv["session"].trace
    turns = []
    for i, t in enumerate(conv["turns"]):
        end = conv["turns"][i + 1]["trace_from"] if i + 1 < len(conv["turns"]) else len(trace)
        turns.append({k: t[k] for k in ("question", "answer", "status", "error")} | {"trace": _slim(trace[t["trace_from"]:end])})
    return {"conversation_id": cid, "stage": conv["stage"], "role": conv["role"], "turns": turns}


# ------------------------------------------------- the insurer's existing source APIs

@app.get("/api/source/policy-status/{policy_id}")
def policy_status(policy_id: str):
    return sources.policy_status(policy_id)


@app.get("/api/source/fraud-score/{claim_id}")
def fraud_score(claim_id: str):
    return sources.fraud_score_live(claim_id)
