"""The operational APIs the insurer already runs, before any AI-ready layer exists."""

from decimal import Decimal

import psycopg
from psycopg.rows import dict_row

from claims import config

SQL_AGENT_DSN = config.PG_DSN.replace("ops:ops-pass", "sql_agent:sql-agent")


def policy_status(policy_id: str) -> dict:
    """Policy Admin API: the policy as it stands right now. It has no notion of 'as of'."""
    with psycopg.connect(config.PG_DSN, row_factory=dict_row) as conn:
        row = conn.execute("SELECT * FROM policy_admin.policies WHERE pol_no = %s", (policy_id,)).fetchone()
    if not row:
        return {"policy_id": policy_id, "found": False}
    return {"policy_id": row["pol_no"], "status": row["status"], "product": row["product"],
            "vehicle": f"{row['make_model']} {row['veh_reg']}", "idv_inr": float(row["idv"]),
            "zero_depreciation_addon": row["zero_dep"], "deductible_inr": float(row["deductible"]),
            "term": f"{row['start_dt']} to {row['end_dt']}", "as_of": "now"}


def fraud_score_live(claim_id: str) -> dict:
    """Fraud engine: recomputes from raw claims rows on every call, with no graph features."""
    from claims.layers.f_signals import fraud_score
    with psycopg.connect(config.PG_DSN, row_factory=dict_row) as conn:
        row = conn.execute("SELECT * FROM claims_db.claims WHERE claim_no = %s", (claim_id,)).fetchone()
    if not row:
        return {"claim_id": claim_id, "found": False}
    score = fraud_score({"shared_contact_claims_30d": 0, "shop_claims_30d": 0, "contact_phone_not_claimant": 0,
                         "post_loss_endorsement": 0})
    return {"claim_id": claim_id, "fraud_score": score, "computed": "on request", "stored": False,
            "note": "computed from the claims row only"}


def run_raw_sql(sql: str, limit: int = 50) -> dict:
    with psycopg.connect(SQL_AGENT_DSN, row_factory=dict_row) as conn:
        cur = conn.execute(sql)
        rows = cur.fetchmany(limit) if cur.description else []
    return {"rows": [{k: float(v) if isinstance(v, Decimal) else v for k, v in r.items()} for r in rows],
            "row_count": len(rows)}


def raw_schema(sample_rows: int = 3) -> list[dict]:
    """What a typical SQL agent puts in its prompt: every table, its columns and a few sample rows."""
    out = []
    with psycopg.connect(SQL_AGENT_DSN, row_factory=dict_row) as conn:
        tables = conn.execute("""SELECT table_schema, table_name FROM information_schema.tables
                                 WHERE table_schema IN ('claims_db','crm') ORDER BY 1, 2""").fetchall()
        for t in tables:
            name = f"{t['table_schema']}.{t['table_name']}"
            cols = conn.execute("""SELECT column_name, data_type FROM information_schema.columns
                                   WHERE table_schema=%s AND table_name=%s ORDER BY ordinal_position""",
                                (t["table_schema"], t["table_name"])).fetchall()
            sample = conn.execute(f"SELECT * FROM {name} LIMIT {sample_rows}").fetchall()
            out.append({"table": name, "columns": {c["column_name"]: c["data_type"] for c in cols},
                        "sample_rows": [{k: str(v) for k, v in r.items()} for r in sample]})
    return out
