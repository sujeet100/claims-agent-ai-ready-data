// Neo4j Browser queries for the talk. Paste one at a time at http://localhost:7480
// (neo4j / claims-graph). Star them to keep them in the sidebar.

// 1 · What links CLM-88421 to other claims? (the ring: one shop, one phone, thirty days)
MATCH (c:Claim {id:'CLM-88421'})-[r1:REPAIRED_AT|CONTACT_PHONE]->(hub)<-[r2:REPAIRED_AT|CONTACT_PHONE]-(o:Claim)
WHERE abs(duration.inDays(o.filed_date, c.filed_date).days) <= 30
RETURN c, r1, hub, r2, o;

// 2 · Who says so? Every link carries its evidence: document, page, raw text, rule, confidence
MATCH (c:Claim)-[r:REPAIRED_AT|CONTACT_PHONE]->(hub)
WHERE c.id IN ['CLM-88421','CLM-77214','CLM-55831']
RETURN c.id AS claim, type(r) AS link, coalesce(hub.name, hub.masked) AS entity,
       coalesce(r.locator, r.evidence) AS evidence, coalesce(r.raw, '') AS written_as, r.method AS method, r.confidence AS confidence;

// 3 · Five spellings, one repair shop (entity resolution from documents to the registry)
MATCH (d:Document)-[m:MENTIONS]->(s:RepairShop {name:'FixIt Motors'})
RETURN DISTINCT m.raw AS written_as, m.rule AS rule, m.confidence AS confidence, count(d) AS documents
ORDER BY confidence;

// 4 · One person, two CRM records (identity resolution)
MATCH (sr:SourceRecord)-[x:RESOLVES_TO]->(p:Person)<-[:FILED_BY]-(:Claim {id:'CLM-88421'})
RETURN sr, x, p;

// 5 · Where does the shared phone live, and who may see it? (instance → metadata → governance)
MATCH path = (:Claim {id:'CLM-88421'})-[:CONTACT_PHONE]->(:Phone)-[:STORED_IN]->(:Column)-[:CLASSIFIED_AS]->(:Classification)-[:VISIBLE_TO]->(:Role)
RETURN path;

// 6 · The claim in its enterprise context: stored in, means, governed by, signals
MATCH (c:Claim {id:'CLM-88421'})-[r:RECORDED_IN|INSTANCE_OF|HAS_SIGNAL|COVERED_BY]->(n)
OPTIONAL MATCH (n)-[l:LIVE_VIEW|DERIVED_FROM|OWNED_BY]->(m)
RETURN c, r, n, l, m;

// 7 · Lineage of the fraud signal back to the source systems
MATCH path = (:Dataset {name:'gold.claim_signals'})-[:DERIVED_FROM|INGESTED_FROM*1..6]->(:System)
RETURN path;

// 8 · After AI for Data: links a model proposed and rules validated
MATCH (c:Claim)-[r:REPAIRED_AT|CONTACT_PHONE]->(hub) WHERE r.proposed_by IS NOT NULL
RETURN c.id AS claim, type(r) AS link, coalesce(hub.name, hub.masked) AS entity,
       r.proposed_by AS proposed_by, r.validated_by AS validated_by, r.validation_confidence AS confidence;
