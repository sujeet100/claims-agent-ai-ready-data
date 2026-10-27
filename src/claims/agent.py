"""The claims assessment agent, and the scorecard that checks it against the talk's six faults.

The model, the prompt and the instructions are identical at every stage. Only the tools,
and the data behind them, change. That is the talk's claim, made testable: the answer
changes because the data changed.
"""

import json
import re
import time
from typing import Literal

from pydantic import BaseModel, Field
from pydantic_ai import Agent, UsageLimits

from claims import config, lakehouse, plane
from claims.governance import Principal

# The insurer's claims-handling procedure. It is the same at every stage, including "before":
# the first build had it too, it just had no data that could satisfy it.
INSTRUCTIONS = """You are the claims assessment agent for a motor insurer in India.
The business goal is straight-through processing: pay low-risk claims fast. Fast-track is the default
for a collision claim up to Rs 5,00,000 on a policy in force. Hold or investigate only when your tools show
concrete evidence of risk; do not hold a claim merely because something could not be checked.
Follow the claims-handling procedure, using only your tools:
1. Establish the claim facts: amounts, dates, damage severity.
2. Establish the policy terms that applied on the date of the loss.
3. Read the supporting documents (FIR, repair invoice, survey) for what they say.
4. Check whether the claim is connected to other claims (shared repair shop, phone, vehicle or person).
5. Weigh fraud indicators, including any fraud score.
6. Decide, call submit_decision once, then return your assessment.
Ground every statement in a tool result and cite where each piece of evidence came from.
If a step cannot be done with your tools, say so rather than assume."""


class Evidence(BaseModel):
    source: str = Field(description="Where it came from: table, document file and page, graph, signal or API")
    detail: str = Field(description="What it shows, briefly")


class ClaimAssessment(BaseModel):
    decision: Literal["fast_track", "standard_settlement", "hold_and_route_to_investigation", "request_more_information"]
    recommended_amount_inr: int | None = Field(None, description="Amount you recommend paying, if any")
    settlement_range_inr: list[int] | None = Field(None, description="[low, high] if you have a benchmark range")
    policy_terms_used: str = Field(description="Which policy terms you relied on, and as of which date")
    zero_dep_applies: bool | None = Field(None, description="Does zero-depreciation cover apply to THIS loss, judged by the terms in force on the loss date? null only if you could not establish them")
    severity_reading: str = Field(description="How severe the damage is, and what scale that comes from")
    fraud_score: float | None = Field(None, description="Fraud score if you obtained one, else null")
    linked_claims: list[str] = Field(default_factory=list, description="Other claim IDs linked to this one")
    red_flags: list[str] = Field(default_factory=list)
    evidence: list[Evidence] = Field(default_factory=list)
    summary: str = Field(description="Two to four sentences for the claims officer")


CHAT_INSTRUCTIONS = """You are the claims intelligence assistant for a motor insurer in India, talking with claims staff.
Answer using only your tools; look things up rather than assume. Cite where each fact came from
(table, document file and page, graph link, signal or API). Be concise and plain. If your tools
cannot answer, say what is missing rather than guess."""


# a stalled API call should fail over, not freeze a stage in front of an audience
MODEL_SETTINGS = {"timeout": 90, "max_retries": 2}


def chat_agent(stage: str, model: str | None = None) -> Agent:
    """The free-form assistant behind the chat tab: same tools, same governance, plain-text answers."""
    return Agent(model or config.default_model(), instructions=CHAT_INSTRUCTIONS, output_type=str,
                 deps_type=plane.Session, tools=plane.pydantic_tools(stage), retries=2, model_settings=MODEL_SETTINGS)


def build_agent(stage: str, model: str | None = None) -> Agent:
    return Agent(model or config.default_model(), instructions=INSTRUCTIONS, output_type=ClaimAssessment,
                 deps_type=plane.Session, tools=plane.pydantic_tools(stage), retries=2, model_settings=MODEL_SETTINGS)


def run(stage: str = "F", role: str = "claims_officer", prompt: str = config.CASE_PROMPT, model: str | None = None,
        session: plane.Session | None = None) -> dict:
    """Run the agent once. Pass a session to watch its trace grow while the run is in flight."""
    session = session or plane.Session(stage=stage, principal=Principal(role=role))
    agent = build_agent(stage, model)
    started = time.perf_counter()
    result = agent.run_sync(prompt, deps=session, usage_limits=UsageLimits(request_limit=30))
    assessment = result.output
    usage = result.usage
    if not session.decisions:
        # the system of action executes the agent's decision through the gateway either way
        plane.traced(session, plane.submit_decision, "Action gateway",
                     {"claim_id": config.CASE_CLAIM, "decision": assessment.decision,
                      "amount_inr": assessment.recommended_amount_inr, "rationale": assessment.summary[:200]})
    record = {"stage": stage, "role": role, "model": model or config.default_model(), "prompt": prompt,
              "run_id": session.run_id, "seconds": round(time.perf_counter() - started, 1),
              "tools_available": [fn.__name__ for fn, _ in plane.available(stage)],
              "assessment": assessment.model_dump(), "decisions": session.decisions, "trace": session.trace,
              "usage": {"requests": usage.requests, "input_tokens": usage.input_tokens, "output_tokens": usage.output_tokens}}
    record["scorecard"] = scorecard(record)
    save(record)
    return record


def save(record: dict):
    config.RUNS.mkdir(parents=True, exist_ok=True)
    path = config.RUNS / f"{record['stage']}-{record['role']}.json"
    path.write_text(json.dumps(record, indent=2, default=str, ensure_ascii=False))


def load(stage: str, role: str = "claims_officer") -> dict | None:
    path = config.RUNS / f"{stage}-{role}.json"
    return json.loads(path.read_text()) if path.exists() else None


# ---------------------------------------------------------------------- scorecard

def _truth() -> dict:
    return json.loads(config.GROUND_TRUTH.read_text())


def _case_fir_chunk() -> str | None:
    docs = lakehouse.read("silver.documents")
    fir = docs[(docs.claim_id == config.CASE_CLAIM) & (docs.doc_type == "FIR") & docs.is_latest]
    return f"{fir.iloc[0]['doc_id']}-p2" if len(fir) else None


def _leaks(trace: list[dict], truth: dict) -> list[str]:
    """Other people's personal data that reached the model unmasked."""
    found = []
    blob = "\n".join(t["output"] for t in trace)
    digits_blob = re.sub(r"[\s\-+]", "", blob)
    for d in truth["other_people_phone_digits"]:
        if d in digits_blob:
            found.append(f"phone …{d[-4:]}")
    for name in truth["other_people_names"]:
        if re.search(rf"\b{re.escape(name)}\b", blob):
            found.append(name)
    return found


def scorecard(record: dict) -> list[dict]:
    truth = _truth()
    a = record["assessment"]
    trace = record["trace"]
    tools_used = {t["tool"] for t in trace}
    outputs = "\n".join(t["output"] for t in trace)
    rows = []

    zd = a.get("zero_dep_applies")
    rows.append({"fault": "Policy date", "layer": "A",
                 "without": "Today's policy", "with": "As of the accident date",
                 "pass": zd is False,
                 "detail": "zero-dep correctly treated as not in force on 12 Oct" if zd is False else
                           ("assumed today's zero-dep add-on covers the loss" if zd else "policy terms on the loss date not established")})

    sev = (a.get("severity_reading") or "").lower()
    ok = "major" in sev
    rows.append({"fault": '"Severity"', "layer": "B", "without": "Three meanings", "with": "One owned definition",
                 "pass": ok, "detail": a.get("severity_reading", "")[:140]})

    fir = _case_fir_chunk()
    cited_docs = any(re.search(r"fir|invoice", (e.get("source", "") + e.get("detail", "")), re.I) for e in a.get("evidence", []))
    read = bool(fir and fir in outputs)
    rows.append({"fault": "FIR and invoice", "layer": "C", "without": "Never readable", "with": "Retrieved and cited",
                 "pass": read and cited_docs,
                 "detail": "FIR page 2 retrieved and cited" if read and cited_docs else
                           ("documents only listed or classified" if not read else "retrieved but not cited")})

    leaks = _leaks(trace, truth)
    rows.append({"fault": "Claimant phone", "layer": "D", "without": "Nearly leaked", "with": "Masked inside retrieval",
                 "pass": not leaks,
                 "detail": "no other person's data reached the model unmasked" if not leaks else
                           f"{len(leaks)} unmasked: " + ", ".join(leaks[:4])})

    found = set(a.get("linked_claims") or []) & set(truth["linked_claims"])
    rows.append({"fault": "Linked claims", "layer": "E", "without": "Missed", "with": "Surfaced through the graph",
                 "pass": len(found) == len(truth["linked_claims"]),
                 "detail": f"{len(found)} of {len(truth['linked_claims'])} linked claims found"})

    fs = a.get("fraud_score")
    from_store = "get_signals" in tools_used
    rows.append({"fault": "Fraud score", "layer": "F", "without": "Never called", "with": "Read from the store, versioned",
                 "pass": from_store and fs is not None and abs(fs - truth["fraud_score"]) < 0.005,
                 "detail": f"fraud_score {fs} from gold.claim_signals" if from_store and fs is not None else
                           ("no fraud score used" if fs is None else f"fraud_score {fs}, not from the signal store")})

    paid = [d for d in record["decisions"] if d.get("status") == "queued_for_payout"]
    held = a.get("decision") == "hold_and_route_to_investigation"
    # a hold only counts when it hands the investigator the evidence that matters; holding blind
    # cannot tell a risky claim from a safe one, so it delays everything or nothing
    grounded = bool(found) or fs is not None
    rows.append({"fault": "The claim", "layer": "outcome", "without": "Near payout on a suspect claim",
                 "with": "Held and routed with evidence, a person decides", "pass": held and grounded and not paid,
                 "detail": (f"queued for payout of Rs {paid[0]['amount_inr']:,}" if paid and paid[0].get("amount_inr") else
                            "held, but blind: no linked claims and no fraud score behind it" if held and not grounded else
                            f"decision: {a.get('decision')}")})
    return rows
