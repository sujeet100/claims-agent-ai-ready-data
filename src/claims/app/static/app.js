// The agentic UI: one question, two agents side by side. Plain JS, no build step.
const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const esc = (v) => String(v ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const api = async (path, opts) => {
  const r = await fetch(path, opts);
  if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || r.statusText);
  return r.json();
};
const post = (path, body) => api(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const inr = (n) => "₹" + Number(n).toLocaleString("en-IN");

const LOGO = (name) => `<span class="logo"><img src="/static/logos/${name}.svg" alt="${name}"></span>`;
const GLYPH = {
  file: `<svg viewBox="0 0 24 24" fill="none" stroke="#6E6A66" stroke-width="2"><path d="M14 3H6a1 1 0 0 0-1 1v16a1 1 0 0 0 1 1h12a1 1 0 0 0 1-1V8z"/><path d="M14 3v5h5"/></svg>`,
  api: `<svg viewBox="0 0 24 24" fill="none" stroke="#6E6A66" stroke-width="2"><circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 3"/></svg>`,
  signal: `<svg viewBox="0 0 24 24" fill="none" stroke="#33357D" stroke-width="2.2"><path d="M4 18l5-6 4 3 7-9"/></svg>`,
  gate: `<svg viewBox="0 0 24 24" fill="none" stroke="#2F7F86" stroke-width="2.2"><rect x="5" y="10" width="14" height="10" rx="2"/><path d="M8 10V7a4 4 0 0 1 8 0v3"/></svg>`,
  table: `<svg viewBox="0 0 24 24" fill="none" stroke="#33357D" stroke-width="2"><ellipse cx="12" cy="6" rx="7" ry="3"/><path d="M5 6v12c0 1.7 3.1 3 7 3s7-1.3 7-3V6M5 12c0 1.7 3.1 3 7 3s7-1.3 7-3"/></svg>`,
  book: `<svg viewBox="0 0 24 24" fill="none" stroke="#7147E8" stroke-width="2"><path d="M4 5a2 2 0 0 1 2-2h13v16H6a2 2 0 0 0-2 2z"/><path d="M4 21V5M9 7h6"/></svg>`,
  search: `<svg viewBox="0 0 24 24" fill="none" stroke="#DC244C" stroke-width="2.2"><circle cx="11" cy="11" r="6"/><path d="M20 20l-4.5-4.5"/></svg>`,
};
const icon = (k) => (GLYPH[k] ? GLYPH[k] : `<img src="/static/logos/${k}.svg" alt="${k}">`);

// What each tool call means, in plain words, and which layer made it possible.
const TOOLS = {
  list_tables: { ic: "postgresql", what: "Listed the raw claims tables", why: "Sample rows of every table go into the prompt, other customers included." },
  run_sql: { ic: "postgresql", what: "Ran SQL on the raw claims database", why: "Cryptic legacy columns: severity 3, amount, amt_appr. No meanings." },
  list_uploads: { ic: "file", what: "Listed the uploaded files", why: "File names only." },
  classify_document: { ic: "file", what: "Labelled a PDF", why: "It knows this is an FIR. It cannot read the garage or the phone inside." },
  get_policy_status: { ic: "api", what: "Asked the live policy API", why: "Today's policy only: the zero-dep add-on looks like it applies." },
  list_datasets: { ic: "book", what: "Read what each table and column means", layer: "B", why: "Owned meanings from the catalog: severity_band B2 is major repair, approved is not requested." },
  query_lakehouse: { ic: "duckdb", what: "Queried the lakehouse", layer: "A", why: "Clean, standardised tables; personal data already masked." },
  get_policy_terms: { ic: "table", what: "Policy terms on the accident date", layer: "A", why: "Zero-dep was added the day after the accident, so it does not apply." },
  list_claim_documents: { ic: "file", what: "Found the claim's documents", layer: "A", why: "Registered, owned and versioned: the revised invoice is the latest." },
  search_catalog: { ic: "book", what: "Searched the catalog", layer: "B", why: "Found which data and which definitions answer the question." },
  define_term: { ic: "book", what: "Looked up an owned definition", layer: "B", why: "One meaning, owned by a team, instead of three guesses." },
  search_documents: { ic: "search", what: "Searched the documents by meaning", layer: "C", why: "Passages it can cite, with document and page." },
  trace_lineage: { ic: "book", what: "Traced where the data came from", layer: "D", why: "Every number traceable to its source." },
  investigate_links: { ic: "neo4j", what: "Followed the knowledge graph", layer: "E", why: "Five other claims, same garage and same phone, within 30 days." },
  get_signals: { ic: "signal", what: "Read the versioned fraud score", layer: "F", why: "Computed once with graph features. The live fraud API says 0.02." },
  submit_decision: { ic: "gate", what: "Submitted the decision", why: "" },
};

function parse(out) { try { return JSON.parse(out); } catch { return null; } }

function result(t) {
  const o = parse(t.output);
  if (t.error) return "error: " + t.error.slice(0, 90);
  if (!o) return "";
  switch (t.tool) {
    case "list_tables": return `${o.length} tables, with sample rows`;
    case "run_sql": case "query_lakehouse": return `${o.row_count ?? 0} row${o.row_count === 1 ? "" : "s"}`;
    case "list_uploads": return `${o.length} files`;
    case "classify_document": return `${o.file_name} → ${o.doc_type}, contents not read`;
    case "get_policy_status": return `zero-dep add-on: ${o.zero_depreciation_addon ? "yes" : "no"} · as of now`;
    case "list_datasets": return `${o.length} datasets, each column with its meaning`;
    case "get_policy_terms": return o.summary || "";
    case "list_claim_documents": return o.map((d) => `${d.doc_type}${d.is_latest ? "" : " (old)"}`).join(", ");
    case "search_catalog": return o.slice(0, 3).map((h) => h.name).join(" · ");
    case "define_term": return o[0]?.term ? `${o[0].term}: ${String(o[0].definition).slice(0, 110)}…` : "";
    case "search_documents": {
      const h = o[0]; if (!h) return "no passages";
      const m = h.text.match(/taken to [^.]+|Contact No\.: [^\n]+|Severity band: [^\n]+/);
      return `${h.cite}${m ? ` — “${m[0]}”` : ""}`;
    }
    case "trace_lineage": return `owner ${o.owner}, ${o.paths?.length || 0} paths to source`;
    case "investigate_links": {
      const hubs = (o.this_claim_links || []).map((h) => h.entity).join(" · ");
      return `${o.linked_count} linked claims · ${hubs}`;
    }
    case "get_signals": {
      const f = o.find((s) => s.signal === "fraud_score"), r = o.find((s) => s.signal === "settlement_range");
      return [f && `fraud_score ${f.value} (${f.model_version})`, r && `range ${inr(r.value_low)}–${inr(r.value_high)}`].filter(Boolean).join(" · ");
    }
    case "submit_decision": return o.reason || o.status || "";
    default: return "";
  }
}

function stepHTML(t, i, live) {
  const d = TOOLS[t.tool] || { ic: "api", what: t.tool };
  const tags = [d.layer ? `<span class="badge">${d.layer}</span>` : "",
    t.masked ? `<span class="badge lock">🔒 ${t.masked}</span>` : "",
    t.tool === "list_tables" ? `<span class="badge warn">PII</span>` : ""].join("");
  const asked = Object.keys(t.args || {}).length ? `<div><b>Asked:</b> <code>${esc(JSON.stringify(t.args).slice(0, 160))}</code></div>` : "";
  return `<div class="step ${live ? "live" : ""}" data-i="${i}" data-layer="${d.layer || ""}" data-tool="${t.tool}">
    <span class="ic">${icon(d.ic)}</span>
    <div><div class="what">${esc(d.what)}</div><div class="got">${esc(result(t))}</div></div>
    <span class="tag">${tags}</span>
    <div class="detail">${asked}<div><b>Got:</b> ${esc(result(t))}</div>${d.why ? `<div class="why">${esc(d.why)}</div>` : ""}</div>
  </div>`;
}

function answerHTML(rec) {
  const a = rec.assessment, d = (rec.decisions || [])[0] || {};
  const fast = a.decision === "fast_track" || a.decision === "standard_settlement";
  const verdict = { fast_track: "Fast-track", standard_settlement: "Settle", hold_and_route_to_investigation: "Hold, and route to investigators",
    request_more_information: "Ask for more information" }[a.decision] || a.decision;
  const st = d.status === "queued_for_payout" ? `<span class="st pay">Payout queued</span>`
    : d.status === "pending_approval" ? `<span class="st gate">Needs ${esc(d.approver_role)} approval</span>`
    : d.status ? `<span class="st ok">${esc(d.action === "route_to_investigation" ? "Routed · a person decides" : d.status)}</span>` : "";
  const facts = [];
  if (a.zero_dep_applies != null) facts.push(`Zero-dep applies to this loss: <b>${a.zero_dep_applies ? "yes" : "no"}</b>`);
  facts.push(`Linked claims: <b>${(a.linked_claims || []).length || "none found"}</b>`);
  facts.push(`Fraud score: <b>${a.fraud_score ?? "not available"}</b>`);
  return `<div class="answer"><div class="verdict-row"><span class="verdict ${fast ? "bad" : ""}">${esc(verdict)}${a.recommended_amount_inr ? " · " + inr(a.recommended_amount_inr) : ""}</span>${st}</div>
    <p>${esc(a.summary)}</p><ul>${facts.map((f) => `<li>${f}</li>`).join("")}</ul></div>`;
}

function md(text) {
  const inline = (t) => esc(t).replace(/\*\*(.+?)\*\*/g, "<b>$1</b>").replace(/`([^`]+)`/g, "<code>$1</code>");
  return text.split(/\n{2,}/).map((b) => {
    const lines = b.split("\n");
    if (lines.every((l) => /^\s*([-*]|\d+\.)\s/.test(l))) return `<ul>${lines.map((l) => `<li>${inline(l.replace(/^\s*([-*]|\d+\.)\s/, ""))}</li>`).join("")}</ul>`;
    return `<p>${lines.map(inline).join("<br>")}</p>`;
  }).join("");
}

// ----------------------------------------------------------------------------- panes
const state = { meta: null, role: "claims_officer", panes: [{ stage: "before", cid: null }, { stage: "F", cid: null }], busy: false };
const PANE_LOGOS = { before: ["postgresql"], A: ["iceberg", "duckdb"], B: ["iceberg", "duckdb", "openmetadata"], C: ["iceberg", "openmetadata", "qdrant"],
  D: ["iceberg", "openmetadata", "qdrant"], E: ["iceberg", "openmetadata", "qdrant", "neo4j"], F: ["iceberg", "openmetadata", "qdrant", "neo4j"] };

function paneHead(i) {
  const p = state.panes[i];
  const before = p.stage === "before";
  const title = before ? "Agent-first build" : p.stage === "F" ? "AI-ready data" : `Layers A–${p.stage}`;
  const sub = before ? "raw SQL · live policy API · PDFs it can only label" : "one governed discovery plane over the six layers";
  const opts = state.meta.stages.map((s) => `<option value="${s.id}" ${s.id === p.stage ? "selected" : ""}>${s.id === "before" ? "No layers" : "Layers A–" + s.id}</option>`).join("");
  return `<div class="pane-head"><div><div class="title">${title}</div><div class="sub">${sub}</div></div>
    <div class="logos">${(PANE_LOGOS[p.stage] || []).map(LOGO).join("")}<select data-pane="${i}">${opts}</select></div></div>
    <div class="thread" id="t${i}"><div class="empty">${before ? "Wired straight to the systems the insurer already has." : "Reaches the data only through the governed discovery plane."}</div></div>`;
}

function renderPanes() {
  [0, 1].forEach((i) => {
    const el = $("#p" + i);
    el.className = "pane" + (state.panes[i].stage === "before" ? " before" : "");
    el.innerHTML = paneHead(i);
  });
  $$(".pane-head select").forEach((s) => s.addEventListener("change", () => {
    state.panes[+s.dataset.pane].stage = s.value; state.panes[+s.dataset.pane].cid = null; renderPanes();
  }));
}

function renderThread(i, question, trace, done, rec, chatAnswer, error) {
  const t = $("#t" + i);
  let h = `<div class="q">${esc(question)}</div>`;
  if (trace.length) h += `<div class="steps"><div class="lbl">${done ? "What it did" : "Working…"}</div>${trace.map((s, k) => stepHTML(s, k, !done && k === trace.length - 1)).join("")}</div>`;
  else if (!done) h += `<div class="working">Thinking…</div>`;
  if (error) h += `<div class="answer">${esc(error)}</div>`;
  else if (rec) h += answerHTML(rec);
  else if (chatAnswer) h += `<div class="answer">${md(chatAnswer)}</div>`;
  t.innerHTML = h;
  t.scrollTop = t.scrollHeight;
}

const isClaimQuestion = (q) => /assess claim/i.test(q);

async function replayPaced(i, rec, seconds) {
  const trace = rec.trace, per = (seconds * 1000) / (trace.length + 1);
  for (let k = 0; k <= trace.length; k++) {
    renderThread(i, rec.prompt, trace.slice(0, k), false);
    await sleep(per);
  }
  renderThread(i, rec.prompt, trace, true, rec);
}

async function runPane(i, question, mode = "live", paced = 0) {
  const p = state.panes[i];
  if (isClaimQuestion(question)) {
    const { run_id } = await post("/api/run", { stage: p.stage, role: state.role, mode });
    if (paced) {
      const r = await api("/api/run/" + run_id);
      return replayPaced(i, r.record, paced);
    }
    for (;;) {
      const r = await api("/api/run/" + run_id);
      if (r.status === "done") return renderThread(i, r.record.prompt || question, r.record.trace, true, r.record);
      if (r.status === "error") return renderThread(i, question, [], true, null, null, r.error);
      renderThread(i, question, r.trace || [], false);
      await sleep(500);
    }
  }
  const r = await post("/api/chat", { message: question, conversation_id: p.cid, stage: p.stage, role: state.role });
  p.cid = r.conversation_id;
  for (;;) {
    const c = await api("/api/chat/" + p.cid);
    const turn = c.turns[c.turns.length - 1];
    if (turn.status !== "running") return renderThread(i, question, turn.trace, true, null, turn.answer, turn.error);
    renderThread(i, question, turn.trace, false);
    await sleep(500);
  }
}

async function askBoth(question, mode = "live", paced = [0, 0]) {
  if (state.busy || !question.trim()) return;
  state.busy = true; $("#send").disabled = true; $("#q").value = "";
  try { await Promise.all([0, 1].map((i) => runPane(i, question, mode, paced[i]))); }
  finally { state.busy = false; $("#send").disabled = false; }
}

// ------------------------------------------------------------ step-through (for the talk)
// stepTo(i, "A") focuses the first step that used layer A; stepTo(i, "decision") the answer;
// stepTo(i, "masked") every step where personal data was masked;
// stepTo(i, 3) a step by index; stepOff() clears.
window.stepTo = (i, key) => {
  const t = $("#t" + i);
  t.classList.add("stepping");
  $$(".focus", t).forEach((e) => e.classList.remove("focus"));
  if (key === "masked") {   // every step where the plane masked personal data
    const hits = $$(".step", t).filter((e) => $(".badge.lock", e));
    hits.forEach((e) => e.classList.add("focus"));
    hits[0]?.scrollIntoView({ behavior: "smooth", block: "center" });
    return hits.length > 0;
  }
  let el = key === "decision" ? $(".answer", t)
    : typeof key === "number" ? $(`.step[data-i="${key}"]`, t)
    : $(`.step[data-layer="${key}"]`, t) || $(`.step[data-tool="${key}"]`, t);
  if (!el) return false;
  el.classList.add("focus");
  el.scrollIntoView({ behavior: "smooth", block: "center" });
  return true;
};
window.playRecorded = async (left = 30, right = 30) => {
  $("#q").value = state.meta.prompt;
  await sleep(400);
  return askBoth(state.meta.prompt, "recorded", [left, right]);
};
window.stepOff = () => $$(".thread").forEach((t) => { t.classList.remove("stepping"); $$(".focus", t).forEach((e) => e.classList.remove("focus")); });

// ------------------------------------------------------------------------------- init
$$("#role button").forEach((b) => b.addEventListener("click", () => {
  $$("#role button").forEach((x) => x.classList.toggle("on", x === b));
  state.role = b.dataset.role; state.panes.forEach((p) => (p.cid = null));
}));
$("#bar").addEventListener("submit", (e) => { e.preventDefault(); askBoth($("#q").value); });

(async function init() {
  state.meta = await api("/api/meta");
  $("#model").textContent = state.meta.model;
  renderPanes();
  const sugg = [state.meta.prompt, "What is the contact phone number on the FIR for CLM-77214?", "What does severity band B2 mean?"];
  $("#sugg").innerHTML = sugg.map((s) => `<button>${esc(s.length > 70 ? s.slice(0, 68) + "…" : s)}</button>`).join("");
  $$("#sugg button").forEach((b, k) => b.addEventListener("click", () => { $("#q").value = sugg[k]; $("#q").focus(); }));
  // ?replay=1 shows the last recorded runs of both panes, e.g. to come back to them after visiting a tool.
  // window.playRecorded(seconds) replays them at a steady pace, for a narrated walkthrough.
  if (new URLSearchParams(location.search).get("replay")) await askBoth(state.meta.prompt, "recorded");
})();
