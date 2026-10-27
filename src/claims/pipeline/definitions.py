"""Dagster definitions: one asset group per layer, quality gates as asset checks.

    dagster dev            # UI at http://localhost:3010, asset groups layer_a ... layer_f
    dagster asset materialize --select '*' -m claims.pipeline.definitions

The asset graph in the Dagster UI is the "how each layer is built" view for the talk:
sources on the left, the six layers left to right, every check visible on its asset.
"""

import dagster as dg
import pandas as pd

from claims import ai_for_data, lakehouse, stores
from claims.layers import a_organization as a, b_semantics as b, c_retrieval as c, d_governance as d, e_integration as e, \
    f_signals as f

SOURCES = [
    dg.AssetSpec("claims_db", group_name="sources", kinds={"postgres"}, description="Operational claims system (2011): terse columns, dd/mm/yy dates."),
    dg.AssetSpec("policy_admin", group_name="sources", kinds={"postgres"}, description="Policy admin: current view only; history in an audit log."),
    dg.AssetSpec("crm", group_name="sources", kinds={"postgres"}, description="Customer records, with duplicates."),
    dg.AssetSpec("regional_tracker", group_name="sources", kinds={"csv"}, description="Pune East office's claims spreadsheet."),
    dg.AssetSpec("garage_registry", group_name="sources", kinds={"csv"}, description="Empanelled repair shops by GSTIN."),
    dg.AssetSpec("uploads", group_name="sources", kinds={"pdf"}, description="Claim documents: no owner, no version."),
]


def _md(df: pd.DataFrame, n: int = 5) -> dg.MetadataValue:
    return dg.MetadataValue.md(df.head(n).to_markdown(index=False))


# ------------------------------------------------------------------------------ Layer A

@dg.asset(group_name="layer_a_organization", deps=["claims_db", "policy_admin", "crm"], kinds={"iceberg"},
          description="Bronze: claims DB, policy admin and CRM copied unchanged, stamped with source and ingest time.")
def bronze_operational() -> dg.MaterializeResult:
    return dg.MaterializeResult(metadata={k: v for k, v in a.ingest_postgres().items()})


@dg.asset(group_name="layer_a_organization", deps=["regional_tracker", "garage_registry", "uploads"], kinds={"iceberg"},
          description="Bronze: the regional spreadsheet, the garage registry and an inventory of uploads.")
def bronze_files() -> dg.MaterializeResult:
    return dg.MaterializeResult(metadata={k: v for k, v in a.ingest_files().items()})


@dg.asset(group_name="layer_a_organization", deps=[bronze_operational], kinds={"iceberg"},
          description="One person per human: CRM duplicates resolved by phone, date of birth and surname.")
def silver_customers() -> dg.MaterializeResult:
    people, links = a.build_customers()
    merged = links[links.duplicated("customer_key", keep=False)]
    return dg.MaterializeResult(metadata={"people": len(people), "source_records": len(links),
                                          "merged_records": dg.MetadataValue.md(merged.to_markdown(index=False))})


@dg.asset(group_name="layer_a_organization", deps=[bronze_operational, silver_customers], kinds={"iceberg"},
          description="Every policy version, rebuilt from the audit log, so terms can be read as of any date.")
def silver_policy_versions() -> dg.MaterializeResult:
    df = a.build_policy_versions()
    return dg.MaterializeResult(metadata={"versions": len(df), "P-2231": _md(df[df.policy_id == "P-2231"][
        ["policy_id", "version", "valid_from", "valid_to", "zero_dep", "endorsement_id"]])})


@dg.asset(group_name="layer_a_organization", deps=[bronze_operational, bronze_files, silver_customers, silver_policy_versions],
          kinds={"iceberg"}, description="One row per claim across the claims DB and the tracker; the gate holds rows without a policy.")
def silver_claims() -> dg.MaterializeResult:
    claims, held = a.build_claims()
    return dg.MaterializeResult(metadata={"claims": len(claims), "held_at_gate": len(held),
                                          "both_sources": int(claims.sources.str.contains(",").sum()),
                                          "tracker_only": int((claims.sources == "regional_tracker").sum())})


@dg.asset_check(asset=silver_claims, blocking=True, description="Claim IDs are unique after standardisation.")
def claim_ids_unique() -> dg.AssetCheckResult:
    ids = lakehouse.read("silver.claims").claim_id
    return dg.AssetCheckResult(passed=bool(ids.is_unique), metadata={"duplicates": int(ids.duplicated().sum())})


@dg.asset_check(asset=silver_claims, description="Rows held at the gate are reported, not dropped silently.")
def quality_events_reported() -> dg.AssetCheckResult:
    q = lakehouse.read("silver.quality_events")
    return dg.AssetCheckResult(passed=True, severity=dg.AssetCheckSeverity.WARN,
                               metadata={"held": len(q), "events": dg.MetadataValue.md(q.to_markdown(index=False)) if len(q) else "none"})


@dg.asset(group_name="layer_a_organization", deps=[bronze_files], kinds={"iceberg"},
          description="Document register: claim, type, version and owner for every upload, read from the content.")
def silver_documents() -> dg.MaterializeResult:
    docs = a.register_documents()
    return dg.MaterializeResult(metadata={"documents": len(docs), "superseded": int((~docs.is_latest).sum()),
                                          "by_type": dg.MetadataValue.json(docs.doc_type.value_counts().to_dict())})


@dg.asset_check(asset=silver_documents, blocking=True, description="Every document has a claim and an owner.")
def documents_owned() -> dg.AssetCheckResult:
    docs = lakehouse.read("silver.documents")
    bad = docs[docs.claim_id.isna() | docs.owner.isna()]
    return dg.AssetCheckResult(passed=len(bad) == 0, metadata={"unlinked": len(bad)})


@dg.asset(group_name="layer_a_organization", deps=[bronze_files], kinds={"iceberg"}, description="Repair shops keyed by GSTIN.")
def silver_repair_shops() -> dg.MaterializeResult:
    return dg.MaterializeResult(metadata={"shops": len(a.build_repair_shops())})


LAYER_A = [silver_claims, silver_customers, silver_policy_versions, silver_documents, silver_repair_shops]

# ------------------------------------------------------------------------------ Layer B


@dg.asset(group_name="layer_b_semantics", deps=LAYER_A, kinds={"yaml", "iceberg"},
          description="Catalog: glossary and column meanings from catalog-as-code; missing meanings drafted by a model for review.")
def catalog() -> dg.MaterializeResult:
    return dg.MaterializeResult(metadata=b.build_catalog(use_llm=True))


@dg.asset_check(asset=catalog, description="Every column has a meaning; AI-drafted ones await review.")
def columns_described() -> dg.AssetCheckResult:
    cols = lakehouse.read("meta.columns")
    missing = cols[cols.description.isna()]
    review = cols[cols.review_status == "needs_review"]
    return dg.AssetCheckResult(passed=len(missing) == 0, severity=dg.AssetCheckSeverity.WARN,
                               metadata={"undescribed": len(missing), "needs_review": len(review)})


@dg.asset(group_name="layer_b_semantics", deps=[catalog], kinds={"neo4j"},
          description="Metadata graph: systems, datasets, columns, glossary terms, owners and lineage.")
def metadata_graph() -> dg.MaterializeResult:
    return dg.MaterializeResult(metadata=b.build_metadata_graph())


@dg.asset(group_name="layer_b_semantics", deps=[catalog], kinds={"qdrant"},
          description="Discovery index: what data and which tool answers what, searchable by meaning.")
def discovery_index() -> dg.MaterializeResult:
    return dg.MaterializeResult(metadata=b.build_discovery_index())


# ------------------------------------------------------------------------------ Layer C

@dg.asset(group_name="layer_c_retrieval", deps=[silver_documents], kinds={"iceberg"},
          description="Page text and template-parsed fields from every latest document.")
def document_text() -> dg.MaterializeResult:
    pages, fields = c.parse_documents()
    return dg.MaterializeResult(metadata={"pages": len(pages), "fields": len(fields)})


@dg.asset(group_name="layer_c_retrieval", deps=[document_text], kinds={"iceberg"},
          description="Section-aware chunks with a context header for citation.")
def document_chunks() -> dg.MaterializeResult:
    ch = c.chunk_documents()
    return dg.MaterializeResult(metadata={"chunks": len(ch), "sample": dg.MetadataValue.md(f"```\n{ch.iloc[0].text[:500]}\n```")})


@dg.asset(group_name="layer_c_retrieval", deps=[document_chunks], kinds={"qdrant"},
          description="Embeddings (local bge-small) indexed with document, page and section.")
def document_index() -> dg.MaterializeResult:
    return dg.MaterializeResult(metadata={"indexed": c.index_chunks(lakehouse.read("silver.document_chunks"))})


@dg.asset_check(asset=document_index, blocking=True, description="Every chunk is indexed and carries its source.")
def chunks_indexed() -> dg.AssetCheckResult:
    n = stores.qdrant().count("documents").count
    expected = len(lakehouse.read("silver.document_chunks"))
    return dg.AssetCheckResult(passed=n == expected, metadata={"indexed": n, "chunks": expected})


# ------------------------------------------------------------------------------ Layer D

@dg.asset(group_name="layer_d_governance", deps=[catalog, document_chunks], kinds={"iceberg"},
          description="Personal data classified on columns (declared) and in document chunks (detected).")
def classifications() -> dg.MaterializeResult:
    cls = d.classify()
    return dg.MaterializeResult(metadata={"by_class": dg.MetadataValue.json(cls.classification.value_counts().to_dict())})


@dg.asset(group_name="layer_d_governance", deps=[classifications, document_index], kinds={"qdrant"},
          description="Classifications written onto every vector payload, so retrieval can enforce them.")
def retrieval_governance() -> dg.MaterializeResult:
    return dg.MaterializeResult(metadata={"tagged": d.tag_chunks(lakehouse.read("meta.classifications"))})


@dg.asset(group_name="layer_d_governance", kinds={"yaml", "iceberg"}, description="policies.yaml compiled into access and action rules.")
def access_policies() -> dg.MaterializeResult:
    return dg.MaterializeResult(metadata={"rules": dg.MetadataValue.md(d.compile_policies().to_markdown(index=False))})


@dg.asset(group_name="layer_d_governance", deps=[classifications, access_policies, metadata_graph], kinds={"neo4j"},
          description="Governance graph: CLASSIFIED_AS, VISIBLE_TO, REQUIRES_APPROVAL_BY.")
def governance_graph() -> dg.MaterializeResult:
    return dg.MaterializeResult(metadata=d.build_governance_graph(lakehouse.read("meta.classifications")))


@dg.asset_check(asset=governance_graph, blocking=True, description="Every classification has an access rule.")
def pii_has_policy() -> dg.AssetCheckResult:
    used = set(lakehouse.read("meta.classifications").classification)
    ruled = set(d.policies()["classifications"])
    return dg.AssetCheckResult(passed=used <= ruled, metadata={"unruled": sorted(used - ruled)})


# ------------------------------------------------------------------------------ Layer E

@dg.asset(group_name="layer_e_integration", deps=[silver_claims, silver_customers, silver_repair_shops, document_text],
          kinds={"iceberg"}, description="Every place an entity appears, in a record or on a document page.")
def entity_mentions() -> dg.MaterializeResult:
    m = e.collect_mentions()
    return dg.MaterializeResult(metadata={"mentions": len(m), "by_type": dg.MetadataValue.json(m.entity_type.value_counts().to_dict())})


@dg.asset(group_name="layer_e_integration", deps=[entity_mentions], kinds={"iceberg"},
          description="Each mention resolved to one entity, by a named rule with a confidence.")
def entity_resolution() -> dg.MaterializeResult:
    r = e.resolve_mentions()
    shops = lakehouse.read("silver.entity_mentions").merge(r, on="mention_id")
    shops = shops[(shops.entity_type == "RepairShop") & (shops.method != "record")].drop_duplicates("raw_value")
    return dg.MaterializeResult(metadata={"resolved": int(r.entity_id.notna().sum()), "unresolved": int(r.entity_id.isna().sum()),
                                          "shop_spellings": dg.MetadataValue.md(shops[["raw_value", "entity_id", "rule", "confidence"]]
                                                                                .to_markdown(index=False))})


@dg.asset(group_name="layer_e_integration", deps=[entity_resolution, silver_policy_versions, silver_documents, governance_graph],
          kinds={"neo4j"}, description="The enterprise knowledge graph: entities, identity, evidence-carrying links, bridged to metadata and governance.")
def knowledge_graph() -> dg.MaterializeResult:
    return dg.MaterializeResult(metadata={k: dg.MetadataValue.json(v) if isinstance(v, dict) else v for k, v in e.build_graph().items()})


@dg.asset_check(asset=knowledge_graph, blocking=True, description="Every link derived from a document carries its evidence.")
def links_have_evidence() -> dg.AssetCheckResult:
    bad = stores.cypher("MATCH (:Claim)-[r:CONTACT_PHONE|REPAIRED_AT]->() WHERE r.method IS NULL OR "
                        "(r.locator IS NULL AND r.evidence IS NULL) RETURN count(r) AS n")[0]["n"]
    return dg.AssetCheckResult(passed=bad == 0, metadata={"links_without_evidence": bad})


# ------------------------------------------------------------------------------ Layer F

@dg.asset(group_name="layer_f_signals", deps=[knowledge_graph, silver_claims, silver_policy_versions], kinds={"iceberg"},
          description="Fraud-model features, two of them computed over the knowledge graph.")
def claim_features() -> dg.MaterializeResult:
    df = f.build_features()
    return dg.MaterializeResult(metadata={"claims": len(df), "CLM-88421": _md(df[df.claim_id == "CLM-88421"])})


@dg.asset(group_name="layer_f_signals", deps=[silver_claims, silver_policy_versions], kinds={"iceberg"},
          description="Approved-amount distribution by segment, loss type and severity band.")
def settlement_benchmarks() -> dg.MaterializeResult:
    return dg.MaterializeResult(metadata={"groups": len(f.build_benchmarks())})


@dg.asset(group_name="layer_f_signals", deps=[claim_features, settlement_benchmarks], kinds={"iceberg"},
          description="fraud_score (fraud-model 3.2) and settlement_range, computed once, versioned, served to every consumer.")
def claim_signals() -> dg.MaterializeResult:
    s = f.build_signals()
    f.link_signals_to_graph(s)
    return dg.MaterializeResult(metadata={"signals": len(s), "CLM-88421": _md(s[s.claim_id == "CLM-88421"][
        ["signal", "value", "value_low", "value_high", "version", "model_version"]])})


@dg.asset_check(asset=claim_signals, blocking=True, description="Every claim has a fraud score within its freshness SLA.")
def signals_fresh() -> dg.AssetCheckResult:
    s = lakehouse.read("gold.claim_signals")
    fs = s[s.signal == "fraud_score"]
    missing = len(set(lakehouse.read("silver.claims").claim_id) - set(fs.claim_id))
    stale = int((pd.to_datetime(fs.valid_until) < pd.Timestamp.utcnow().tz_localize(None)).sum())
    return dg.AssetCheckResult(passed=missing == 0 and stale == 0, metadata={"unscored": missing, "stale": stale})


# --------------------------------------------------- the catalog people browse (optional)

@dg.asset(group_name="openmetadata", deps=[catalog, governance_graph, claim_signals, document_chunks], kinds={"openmetadata"},
          description="Owners, glossary, PII tags, column meanings and lineage pushed to OpenMetadata, when it is running.")
def openmetadata_catalog() -> dg.MaterializeResult:
    from claims import openmetadata
    if not openmetadata.available():
        return dg.MaterializeResult(metadata={"skipped": "OpenMetadata not reachable; start it with docker compose --profile catalog up -d"})
    return dg.MaterializeResult(metadata=openmetadata.sync())


# --------------------------------------------------------------------- AI for Data job

@dg.op(description="The seventh claim: ingest, extract with a model, propose, validate, alert.")
def seventh_claim_op(context: dg.OpExecutionContext):
    for ev in ai_for_data.run():
        context.log.info(f"[{ev['step']}] {ev['detail']}")


@dg.job(description="AI for Data: a new free-form FIR is read by a model, its links validated and added to the graph.")
def seventh_claim():
    seventh_claim_op()


build_everything = dg.define_asset_job("build_all_layers", selection=dg.AssetSelection.all() - dg.AssetSelection.groups("sources"))

defs = dg.Definitions(
    assets=[*SOURCES, bronze_operational, bronze_files, silver_customers, silver_policy_versions, silver_claims, silver_documents,
            silver_repair_shops, catalog, metadata_graph, discovery_index, document_text, document_chunks, document_index,
            classifications, retrieval_governance, access_policies, governance_graph, entity_mentions, entity_resolution,
            knowledge_graph, claim_features, settlement_benchmarks, claim_signals, openmetadata_catalog],
    asset_checks=[claim_ids_unique, quality_events_reported, documents_owned, columns_described, chunks_indexed, pii_has_policy,
                  links_have_evidence, signals_fresh],
    jobs=[build_everything, seventh_claim],
    executor=dg.in_process_executor,   # the Iceberg SQL catalog is SQLite: one writer at a time
)
