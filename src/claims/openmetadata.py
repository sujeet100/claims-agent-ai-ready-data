"""Push the System of Understanding into OpenMetadata, the catalog people browse.

Catalog-as-code (catalog/*.yaml) stays the source of truth and the pipeline keeps the
lakehouse meta.* tables and the metadata graph; this sync makes the same meaning visible in
OpenMetadata: owners as teams, the glossary, a ClaimsPII classification, every source and
lakehouse table with column meanings, terms and tags, and lineage from the operational
systems through bronze, silver and gold. AI-drafted meanings carry a Review.AIDrafted tag.

The agent does not query OpenMetadata. As the paper says, the catalog informs the
discovery plane; it is not the agent's query surface.
"""

import base64
import logging
from functools import lru_cache

import httpx
import psycopg
import yaml

from claims import config, lakehouse

log = logging.getLogger(__name__)
API = config.OPENMETADATA_URL.rstrip("/") + "/api/v1"
SERVICE, SOURCE_SERVICE, DB, SOURCE_DB = "claims_lakehouse", "insurer_operational", "lakehouse", "operational"
GLOSSARY, PII, REVIEW = "Claims", "ClaimsPII", "Review"


class OMError(RuntimeError):
    pass


def available() -> bool:
    try:
        return httpx.get(f"{API}/system/version", timeout=3).status_code == 200
    except httpx.HTTPError:
        return False


@lru_cache(maxsize=1)
def _token() -> str:
    r = httpx.post(f"{API}/users/login", timeout=20,
                   json={"email": "admin@open-metadata.org", "password": base64.b64encode(b"admin").decode()})
    if r.status_code != 200:
        raise OMError(f"login failed: {r.status_code} {r.text[:200]}")
    return r.json()["accessToken"]


def _call(method: str, path: str, **kw) -> dict:
    r = httpx.request(method, API + path, headers={"Authorization": f"Bearer {_token()}"}, timeout=30, **kw)
    if r.status_code >= 400:
        raise OMError(f"{method} {path} -> {r.status_code}: {r.text[:300]}")
    return r.json() if r.content else {}


def _put(path: str, body: dict) -> dict:
    return _call("PUT", path, json=body)


def _fqn(*parts: str) -> str:
    return ".".join(f'"{p}"' if "." in p else p for p in parts)


ARROW_TO_OM = {"string": "VARCHAR", "large_string": "VARCHAR", "int64": "BIGINT", "int32": "INT", "double": "DOUBLE",
               "float": "FLOAT", "bool": "BOOLEAN", "date32[day]": "DATE", "timestamp[us]": "TIMESTAMP"}
PG_TO_OM = {"text": "TEXT", "integer": "INT", "numeric": "NUMERIC", "date": "DATE", "boolean": "BOOLEAN", "jsonb": "JSON",
            "timestamp without time zone": "TIMESTAMP"}


def _column(name: str, data_type: str, description: str | None = None, tags: list[dict] | None = None) -> dict:
    col = {"name": name, "dataType": data_type, "tags": tags or []}
    if data_type in ("VARCHAR", "TEXT"):
        col["dataLength"] = 1
    if description:
        col["description"] = description
    return col


def _tag(fqn: str, source: str = "Classification") -> dict:
    return {"tagFQN": fqn, "source": source, "labelType": "Manual", "state": "Confirmed"}


def sync() -> dict:
    datasets = yaml.safe_load((config.CATALOG_AS_CODE / "datasets.yaml").read_text())
    stats = {"teams": 0, "terms": 0, "tables": 0, "lineage_edges": 0}

    # owners
    team_ids = {}
    owners = {d["owner"] for d in datasets["datasets"].values()} | {s["owner"] for s in datasets["systems"].values()}
    owners |= set(lakehouse.read("meta.glossary").owner)
    for owner in sorted(owners):
        team_ids[owner] = _put("/teams", {"name": owner.replace(" ", "_"), "displayName": owner, "teamType": "Group"})["id"]
    stats["teams"] = len(team_ids)
    owner_ref = lambda name: [{"id": team_ids[name], "type": "team"}] if name in team_ids else []   # noqa: E731

    # classifications: personal data, and a review flag for AI-drafted meanings
    _put("/classifications", {"name": PII, "description": "Personal data in claims, from catalog/policies.yaml. "
                                                         "Visible to investigators only; masked for everyone else."})
    policies = yaml.safe_load((config.CATALOG_AS_CODE / "policies.yaml").read_text())
    for cls, rule in policies["classifications"].items():
        _put("/tags", {"classification": PII, "name": cls.split(".")[1],
                       "description": f"{cls}: visible to {', '.join(rule['visible_to'])}; masked as {rule['mask']} otherwise."})
    _put("/classifications", {"name": REVIEW, "description": "Review state of catalog content."})
    _put("/tags", {"classification": REVIEW, "name": "AIDrafted", "description": "Meaning drafted by a model during the build; awaiting review by the owner."})

    # glossary
    _put("/glossaries", {"name": GLOSSARY, "displayName": "Claims glossary",
                         "description": "Owned business definitions for motor claims, from catalog/glossary.yaml."})
    for t in lakehouse.read("meta.glossary").to_dict("records"):
        _put("/glossaryTerms", {"glossary": GLOSSARY, "name": t["term"], "description": t["definition"],
                                "synonyms": [s for s in t["synonyms"].split(",") if s], "owners": owner_ref(t["owner"])})
        stats["terms"] += 1

    # operational sources, so lineage starts where the data really starts
    _put("/services/databaseServices", {"name": SOURCE_SERVICE, "serviceType": "CustomDatabase",
                                        "connection": {"config": {"type": "CustomDatabase", "sourcePythonClass": "none"}},
                                        "description": "The insurer's operational Postgres: claims DB, policy admin, CRM."})
    _put("/databases", {"name": SOURCE_DB, "service": SOURCE_SERVICE})
    table_ids = {}
    with psycopg.connect(config.PG_DSN) as conn:
        rows = conn.execute("""SELECT table_schema, table_name, column_name, data_type FROM information_schema.columns
                               WHERE table_schema IN ('claims_db','policy_admin','crm') ORDER BY 1, 2, ordinal_position""").fetchall()
    by_table: dict[tuple, list] = {}
    for schema, table, col, dtype in rows:
        by_table.setdefault((schema, table), []).append(_column(col, PG_TO_OM.get(dtype, "VARCHAR")))
    system_of = {"claims_db": "Claims DB", "policy_admin": "Policy Admin", "crm": "CRM"}
    for (schema, table), cols in by_table.items():
        _put("/databaseSchemas", {"name": schema, "database": _fqn(SOURCE_SERVICE, SOURCE_DB)})
        t = _put("/tables", {"name": table, "databaseSchema": _fqn(SOURCE_SERVICE, SOURCE_DB, schema), "columns": cols,
                             "description": f"Source table in {system_of[schema]}. Raw, undocumented.",
                             "owners": owner_ref(datasets["systems"][system_of[schema]]["owner"])})
        table_ids[f"{schema}.{table}"] = t["id"]
        stats["tables"] += 1

    # the lakehouse, with meaning, terms and classifications on every column
    _put("/services/databaseServices", {"name": SERVICE, "serviceType": "CustomDatabase",
                                        "connection": {"config": {"type": "CustomDatabase", "sourcePythonClass": "none"}},
                                        "description": "Apache Iceberg lakehouse (System of Record), queried with DuckDB."})
    _put("/databases", {"name": DB, "service": SERVICE})
    cols = lakehouse.read("meta.columns")
    meaning = {(r.dataset, r.column): r for r in cols.itertuples()}
    for name in lakehouse.tables(("bronze", "silver", "gold")):
        schema, table = name.split(".")
        _put("/databaseSchemas", {"name": schema, "database": _fqn(SERVICE, DB),
                                  "description": {"bronze": "Raw copies, stamped with source and ingest time.",
                                                  "silver": "Standardised, deduplicated, validated, historised.",
                                                  "gold": "Business-ready features, metrics and signals."}[schema]})
        spec = datasets["datasets"].get(name, {})
        om_cols = []
        for field in lakehouse.catalog().load_table(name).schema().as_arrow():
            m = meaning.get((name, field.name))
            tags = []
            if m is not None and isinstance(m.classification, str):
                tags.append(_tag(f"{PII}.{m.classification.split('.')[1]}"))
            if m is not None and isinstance(m.term, str):
                tags.append(_tag(f"{GLOSSARY}.{m.term}", "Glossary"))
            if m is not None and m.review_status == "needs_review":
                tags.append(_tag(f"{REVIEW}.AIDrafted"))
            desc = m.description if m is not None and isinstance(m.description, str) else None
            om_cols.append(_column(field.name, ARROW_TO_OM.get(str(field.type), "VARCHAR"), desc, tags))
        t = _put("/tables", {"name": table, "databaseSchema": _fqn(SERVICE, DB, schema), "columns": om_cols,
                             "description": spec.get("description", "Bronze copy of a source table." if schema == "bronze" else ""),
                             "owners": owner_ref(spec.get("owner", "Data Platform"))})
        table_ids[name] = t["id"]
        stats["tables"] += 1

    # lineage: sources -> bronze -> silver -> gold
    edges = [("claims_db.claims", "bronze.claims_db_claims"), ("claims_db.claim_assessments", "bronze.claims_db_assessments"),
             ("policy_admin.policies", "bronze.policy_admin_policies"), ("policy_admin.policy_audit_log", "bronze.policy_audit_log"),
             ("crm.customers", "bronze.crm_customers")]
    for ds, spec in datasets["datasets"].items():
        edges += [(up, ds) for up in spec.get("upstream", [])]
    for src, dst in edges:
        if src in table_ids and dst in table_ids:
            _put("/lineage", {"edge": {"fromEntity": {"id": table_ids[src], "type": "table"},
                                       "toEntity": {"id": table_ids[dst], "type": "table"}}})
            stats["lineage_edges"] += 1
    return stats
