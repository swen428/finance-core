-- Additive lifecycle generation. Historical migrations and facts are unchanged.
CREATE TABLE l2_sources (
 source_id TEXT PRIMARY KEY, source_key TEXT NOT NULL UNIQUE, evidence_id TEXT NOT NULL UNIQUE,
 principal_id TEXT NOT NULL, client_id TEXT NOT NULL, raw_input TEXT NOT NULL,
 original_sha256 TEXT NOT NULL CHECK(length(original_sha256)=64), source_sha256 TEXT NOT NULL,
 generation TEXT NOT NULL, created_at INTEGER NOT NULL
) STRICT;
CREATE TABLE l2_proposals (
 proposal_id TEXT PRIMARY KEY, source_id TEXT NOT NULL REFERENCES l2_sources(source_id),
 version INTEGER NOT NULL CHECK(version>0), observation_id TEXT NOT NULL UNIQUE,
 expected_version INTEGER NOT NULL, record_id TEXT, values_json TEXT NOT NULL CHECK(json_valid(values_json)),
 observation_json TEXT NOT NULL CHECK(json_valid(observation_json)), proposal_sha256 TEXT NOT NULL,
 created_at INTEGER NOT NULL, UNIQUE(source_id,version)
) STRICT;
CREATE TABLE l2_reviews (
 review_id TEXT PRIMARY KEY, proposal_id TEXT NOT NULL UNIQUE REFERENCES l2_proposals(proposal_id),
 expected_version INTEGER NOT NULL, record_id TEXT, values_json TEXT NOT NULL CHECK(json_valid(values_json)),
 material_json TEXT NOT NULL CHECK(json_valid(material_json)), review_sha256 TEXT NOT NULL UNIQUE,
 created_at INTEGER NOT NULL
) STRICT;
CREATE TABLE l2_deliveries (
 review_id TEXT PRIMARY KEY REFERENCES l2_reviews(review_id), evidence_id TEXT NOT NULL UNIQUE,
 material_json TEXT NOT NULL CHECK(json_valid(material_json))
) STRICT;
CREATE TABLE l2_facts (
 record_id TEXT NOT NULL, version INTEGER NOT NULL CHECK(version>0), economic_event_id TEXT NOT NULL,
 source_id TEXT NOT NULL REFERENCES l2_sources(source_id), review_id TEXT UNIQUE REFERENCES l2_reviews(review_id),
 values_json TEXT NOT NULL CHECK(json_valid(values_json)), predecessor_sha256 TEXT,
 fact_sha256 TEXT NOT NULL UNIQUE, commit_seq INTEGER NOT NULL UNIQUE,
 PRIMARY KEY(record_id,version), UNIQUE(economic_event_id,version),
 CHECK((version=1 AND predecessor_sha256 IS NULL) OR (version>1 AND predecessor_sha256 IS NOT NULL))
) STRICT;
CREATE INDEX l2_current_facts ON l2_facts(record_id,version DESC);
CREATE TABLE l2_decisions (
 action_evidence_id TEXT PRIMARY KEY, review_id TEXT NOT NULL UNIQUE REFERENCES l2_reviews(review_id),
 operation_id TEXT NOT NULL UNIQUE, record_id TEXT NOT NULL, version INTEGER NOT NULL,
 material_json TEXT NOT NULL CHECK(json_valid(material_json)),
 FOREIGN KEY(record_id,version) REFERENCES l2_facts(record_id,version) DEFERRABLE INITIALLY DEFERRED
) STRICT;
CREATE TABLE l2_events (
 seq INTEGER PRIMARY KEY, operation_id TEXT NOT NULL UNIQUE, kind TEXT NOT NULL,
 entity_id TEXT NOT NULL, payload_json TEXT NOT NULL CHECK(json_valid(payload_json)),
 previous_sha256 TEXT, event_sha256 TEXT NOT NULL UNIQUE
) STRICT;
CREATE INDEX l2_events_entity ON l2_events(kind,entity_id);
CREATE UNIQUE INDEX l2_decisions_record ON l2_decisions(record_id,version);
CREATE TABLE l2_imports (
 export_id TEXT PRIMARY KEY, export_sha256 TEXT NOT NULL UNIQUE,
 material_json TEXT NOT NULL CHECK(json_valid(material_json))
) STRICT;
CREATE TABLE l2_archives (
 export_id TEXT NOT NULL REFERENCES l2_imports(export_id), identity TEXT NOT NULL,
 kind TEXT NOT NULL, material_json TEXT NOT NULL CHECK(json_valid(material_json)),
 PRIMARY KEY(export_id,kind,identity)
) STRICT;
CREATE TRIGGER l2_facts_sequence BEFORE INSERT ON l2_facts BEGIN
 SELECT RAISE(ABORT,'lifecycle predecessor mismatch') WHERE
 NEW.version != coalesce((SELECT max(version) FROM l2_facts WHERE record_id=NEW.record_id),0)+1
 OR (NEW.version>1 AND NOT EXISTS(SELECT 1 FROM l2_facts WHERE record_id=NEW.record_id
 AND version=NEW.version-1 AND fact_sha256=NEW.predecessor_sha256
 AND economic_event_id=NEW.economic_event_id AND source_id=NEW.source_id));
END;
CREATE TRIGGER l2_sources_no_update BEFORE UPDATE ON l2_sources BEGIN SELECT RAISE(ABORT,'lifecycle evidence is append only'); END;
CREATE TRIGGER l2_sources_no_delete BEFORE DELETE ON l2_sources BEGIN SELECT RAISE(ABORT,'lifecycle evidence is append only'); END;
CREATE TRIGGER l2_sources_no_collision BEFORE INSERT ON l2_sources WHEN EXISTS(SELECT 1 FROM l2_sources WHERE source_id=NEW.source_id OR source_key=NEW.source_key OR evidence_id=NEW.evidence_id) BEGIN SELECT RAISE(ABORT,'lifecycle identity collision'); END;
CREATE TRIGGER l2_proposals_no_update BEFORE UPDATE ON l2_proposals BEGIN SELECT RAISE(ABORT,'lifecycle evidence is append only'); END;
CREATE TRIGGER l2_proposals_no_delete BEFORE DELETE ON l2_proposals BEGIN SELECT RAISE(ABORT,'lifecycle evidence is append only'); END;
CREATE TRIGGER l2_proposals_no_collision BEFORE INSERT ON l2_proposals WHEN EXISTS(SELECT 1 FROM l2_proposals WHERE proposal_id=NEW.proposal_id OR observation_id=NEW.observation_id OR (source_id=NEW.source_id AND version=NEW.version)) BEGIN SELECT RAISE(ABORT,'lifecycle identity collision'); END;
CREATE TRIGGER l2_reviews_no_update BEFORE UPDATE ON l2_reviews BEGIN SELECT RAISE(ABORT,'lifecycle evidence is append only'); END;
CREATE TRIGGER l2_reviews_no_delete BEFORE DELETE ON l2_reviews BEGIN SELECT RAISE(ABORT,'lifecycle evidence is append only'); END;
CREATE TRIGGER l2_reviews_no_collision BEFORE INSERT ON l2_reviews WHEN EXISTS(SELECT 1 FROM l2_reviews WHERE review_id=NEW.review_id OR proposal_id=NEW.proposal_id OR review_sha256=NEW.review_sha256) BEGIN SELECT RAISE(ABORT,'lifecycle identity collision'); END;
CREATE TRIGGER l2_deliveries_no_update BEFORE UPDATE ON l2_deliveries BEGIN SELECT RAISE(ABORT,'lifecycle evidence is append only'); END;
CREATE TRIGGER l2_deliveries_no_delete BEFORE DELETE ON l2_deliveries BEGIN SELECT RAISE(ABORT,'lifecycle evidence is append only'); END;
CREATE TRIGGER l2_deliveries_no_collision BEFORE INSERT ON l2_deliveries WHEN EXISTS(SELECT 1 FROM l2_deliveries WHERE review_id=NEW.review_id OR evidence_id=NEW.evidence_id) BEGIN SELECT RAISE(ABORT,'lifecycle identity collision'); END;
CREATE TRIGGER l2_facts_no_update BEFORE UPDATE ON l2_facts BEGIN SELECT RAISE(ABORT,'lifecycle evidence is append only'); END;
CREATE TRIGGER l2_facts_no_delete BEFORE DELETE ON l2_facts BEGIN SELECT RAISE(ABORT,'lifecycle evidence is append only'); END;
CREATE TRIGGER l2_facts_no_collision BEFORE INSERT ON l2_facts WHEN EXISTS(SELECT 1 FROM l2_facts WHERE (record_id=NEW.record_id AND version=NEW.version) OR (economic_event_id=NEW.economic_event_id AND version=NEW.version) OR fact_sha256=NEW.fact_sha256 OR commit_seq=NEW.commit_seq OR (NEW.review_id IS NOT NULL AND review_id=NEW.review_id)) BEGIN SELECT RAISE(ABORT,'lifecycle identity collision'); END;
CREATE TRIGGER l2_decisions_no_update BEFORE UPDATE ON l2_decisions BEGIN SELECT RAISE(ABORT,'lifecycle evidence is append only'); END;
CREATE TRIGGER l2_decisions_no_delete BEFORE DELETE ON l2_decisions BEGIN SELECT RAISE(ABORT,'lifecycle evidence is append only'); END;
CREATE TRIGGER l2_decisions_no_collision BEFORE INSERT ON l2_decisions WHEN EXISTS(SELECT 1 FROM l2_decisions WHERE action_evidence_id=NEW.action_evidence_id OR review_id=NEW.review_id OR operation_id=NEW.operation_id) BEGIN SELECT RAISE(ABORT,'lifecycle identity collision'); END;
CREATE TRIGGER l2_events_no_update BEFORE UPDATE ON l2_events BEGIN SELECT RAISE(ABORT,'lifecycle evidence is append only'); END;
CREATE TRIGGER l2_events_no_delete BEFORE DELETE ON l2_events BEGIN SELECT RAISE(ABORT,'lifecycle evidence is append only'); END;
CREATE TRIGGER l2_events_no_collision BEFORE INSERT ON l2_events WHEN EXISTS(SELECT 1 FROM l2_events WHERE seq=NEW.seq OR operation_id=NEW.operation_id OR event_sha256=NEW.event_sha256) BEGIN SELECT RAISE(ABORT,'lifecycle identity collision'); END;
CREATE TRIGGER l2_imports_no_update BEFORE UPDATE ON l2_imports BEGIN SELECT RAISE(ABORT,'lifecycle evidence is append only'); END;
CREATE TRIGGER l2_imports_no_delete BEFORE DELETE ON l2_imports BEGIN SELECT RAISE(ABORT,'lifecycle evidence is append only'); END;
CREATE TRIGGER l2_imports_no_collision BEFORE INSERT ON l2_imports WHEN EXISTS(SELECT 1 FROM l2_imports WHERE export_id=NEW.export_id OR export_sha256=NEW.export_sha256) BEGIN SELECT RAISE(ABORT,'lifecycle identity collision'); END;
CREATE TRIGGER l2_archives_no_update BEFORE UPDATE ON l2_archives BEGIN SELECT RAISE(ABORT,'lifecycle evidence is append only'); END;
CREATE TRIGGER l2_archives_no_delete BEFORE DELETE ON l2_archives BEGIN SELECT RAISE(ABORT,'lifecycle evidence is append only'); END;
CREATE TRIGGER l2_archives_no_collision BEFORE INSERT ON l2_archives WHEN EXISTS(SELECT 1 FROM l2_archives WHERE export_id=NEW.export_id AND kind=NEW.kind AND identity=NEW.identity) BEGIN SELECT RAISE(ABORT,'lifecycle identity collision'); END;
