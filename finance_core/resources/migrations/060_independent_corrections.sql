-- Independent correction history. No legacy D2 identity or decision adoption.
CREATE TABLE application_correction_targets (
 target_id TEXT PRIMARY KEY REFERENCES transactions(public_id),
 attempt_id TEXT NOT NULL UNIQUE REFERENCES application_posting_attempts(attempt_id),
 source_json TEXT NOT NULL CHECK(json_valid(source_json)),
 source_hash TEXT NOT NULL CHECK(length(source_hash)=64),
 original_hash TEXT NOT NULL CHECK(length(original_hash)=64)
) STRICT;
CREATE TABLE application_correction_plans (
 plan_id TEXT PRIMARY KEY,
 target_id TEXT NOT NULL REFERENCES application_correction_targets(target_id),
 correction_id TEXT NOT NULL UNIQUE,
 expected_version INTEGER NOT NULL CHECK(expected_version>=0),
 predecessor_id TEXT,
 predecessor_hash TEXT NOT NULL CHECK(length(predecessor_hash)=64),
 material_json TEXT NOT NULL CHECK(json_valid(material_json)),
 plan_hash TEXT NOT NULL UNIQUE CHECK(length(plan_hash)=64),
 created_at INTEGER NOT NULL CHECK(created_at>0),
 expires_at INTEGER NOT NULL CHECK(expires_at>created_at AND expires_at<=created_at+600),
 UNIQUE(plan_id,target_id,correction_id),
 CHECK((expected_version=0 AND predecessor_id IS NULL) OR
       (expected_version>0 AND predecessor_id IS NOT NULL))
) STRICT;
CREATE TABLE application_correction_versions (
 correction_id TEXT PRIMARY KEY,
 target_id TEXT NOT NULL REFERENCES application_correction_targets(target_id),
 version INTEGER NOT NULL CHECK(version>0),
 predecessor_id TEXT,
 predecessor_hash TEXT NOT NULL CHECK(length(predecessor_hash)=64),
 plan_id TEXT NOT NULL UNIQUE,
 result_json TEXT NOT NULL CHECK(json_valid(result_json)),
 result_hash TEXT NOT NULL UNIQUE CHECK(length(result_hash)=64),
 UNIQUE(target_id,version),
 UNIQUE(correction_id,plan_id,target_id),
 FOREIGN KEY(plan_id,target_id,correction_id)
   REFERENCES application_correction_plans(plan_id,target_id,correction_id),
 FOREIGN KEY(correction_id,plan_id,target_id)
   REFERENCES application_correction_decisions(correction_id,plan_id,target_id)
   DEFERRABLE INITIALLY DEFERRED,
 FOREIGN KEY(predecessor_id) REFERENCES application_correction_versions(correction_id),
 CHECK((version=1 AND predecessor_id IS NULL) OR (version>1 AND predecessor_id IS NOT NULL))
) STRICT;
CREATE TABLE application_correction_decisions (
 correction_id TEXT PRIMARY KEY,
 plan_id TEXT NOT NULL UNIQUE,
 target_id TEXT NOT NULL,
 decision_namespace TEXT NOT NULL,
 decision_id TEXT NOT NULL,
 decision_digest TEXT NOT NULL UNIQUE CHECK(length(decision_digest)=64),
 material_json TEXT NOT NULL CHECK(json_valid(material_json)),
 accepted_at INTEGER NOT NULL CHECK(accepted_at>0),
 UNIQUE(decision_namespace,decision_id),
 UNIQUE(correction_id,plan_id,target_id),
 FOREIGN KEY(correction_id,plan_id,target_id)
   REFERENCES application_correction_versions(correction_id,plan_id,target_id)
   DEFERRABLE INITIALLY DEFERRED
) STRICT;
CREATE TABLE application_correction_receipt_facts (
 fact_id TEXT PRIMARY KEY,
 correction_id TEXT NOT NULL UNIQUE REFERENCES application_correction_versions(correction_id),
 target_id TEXT NOT NULL REFERENCES application_correction_targets(target_id),
 snapshot_id TEXT NOT NULL UNIQUE REFERENCES authoritative_calculation_snapshots(snapshot_public_id),
 material_json TEXT NOT NULL CHECK(json_valid(material_json))
) STRICT;
CREATE TRIGGER application_correction_versions_sequence BEFORE INSERT ON application_correction_versions
BEGIN
 SELECT RAISE(ABORT,'independent correction predecessor is not current') WHERE
 NEW.version != 1 + (SELECT count(*) FROM application_correction_versions WHERE target_id=NEW.target_id)
 OR (NEW.version=1 AND NEW.predecessor_hash!=(SELECT original_hash FROM application_correction_targets WHERE target_id=NEW.target_id))
 OR (NEW.version>1 AND NOT EXISTS(SELECT 1 FROM application_correction_versions WHERE
 target_id=NEW.target_id AND version=NEW.version-1 AND correction_id=NEW.predecessor_id AND result_hash=NEW.predecessor_hash));
END;
CREATE TRIGGER application_correction_targets_no_update BEFORE UPDATE ON application_correction_targets BEGIN SELECT RAISE(ABORT,'independent correction history is immutable'); END;
CREATE TRIGGER application_correction_targets_no_delete BEFORE DELETE ON application_correction_targets BEGIN SELECT RAISE(ABORT,'independent correction history is immutable'); END;
CREATE TRIGGER application_correction_targets_no_collision BEFORE INSERT ON application_correction_targets WHEN EXISTS(SELECT 1 FROM application_correction_targets WHERE target_id=NEW.target_id OR attempt_id=NEW.attempt_id) BEGIN SELECT RAISE(ABORT,'independent correction identity collision'); END;
CREATE TRIGGER application_correction_plans_no_update BEFORE UPDATE ON application_correction_plans BEGIN SELECT RAISE(ABORT,'independent correction history is immutable'); END;
CREATE TRIGGER application_correction_plans_no_delete BEFORE DELETE ON application_correction_plans BEGIN SELECT RAISE(ABORT,'independent correction history is immutable'); END;
CREATE TRIGGER application_correction_plans_no_collision BEFORE INSERT ON application_correction_plans WHEN EXISTS(SELECT 1 FROM application_correction_plans WHERE plan_id=NEW.plan_id OR correction_id=NEW.correction_id OR plan_hash=NEW.plan_hash) BEGIN SELECT RAISE(ABORT,'independent correction identity collision'); END;
CREATE TRIGGER application_correction_decisions_no_update BEFORE UPDATE ON application_correction_decisions BEGIN SELECT RAISE(ABORT,'independent correction history is immutable'); END;
CREATE TRIGGER application_correction_decisions_no_delete BEFORE DELETE ON application_correction_decisions BEGIN SELECT RAISE(ABORT,'independent correction history is immutable'); END;
CREATE TRIGGER application_correction_decisions_no_collision BEFORE INSERT ON application_correction_decisions WHEN EXISTS(SELECT 1 FROM application_correction_decisions WHERE correction_id=NEW.correction_id OR plan_id=NEW.plan_id OR decision_digest=NEW.decision_digest OR (decision_namespace=NEW.decision_namespace AND decision_id=NEW.decision_id)) BEGIN SELECT RAISE(ABORT,'independent correction identity collision'); END;
CREATE TRIGGER application_correction_versions_no_update BEFORE UPDATE ON application_correction_versions BEGIN SELECT RAISE(ABORT,'independent correction history is immutable'); END;
CREATE TRIGGER application_correction_versions_no_delete BEFORE DELETE ON application_correction_versions BEGIN SELECT RAISE(ABORT,'independent correction history is immutable'); END;
CREATE TRIGGER application_correction_versions_no_collision BEFORE INSERT ON application_correction_versions WHEN EXISTS(SELECT 1 FROM application_correction_versions WHERE correction_id=NEW.correction_id OR plan_id=NEW.plan_id OR result_hash=NEW.result_hash OR (target_id=NEW.target_id AND version=NEW.version)) BEGIN SELECT RAISE(ABORT,'independent correction identity collision'); END;
CREATE TRIGGER application_correction_receipt_facts_no_update BEFORE UPDATE ON application_correction_receipt_facts BEGIN SELECT RAISE(ABORT,'independent correction history is immutable'); END;
CREATE TRIGGER application_correction_receipt_facts_no_delete BEFORE DELETE ON application_correction_receipt_facts BEGIN SELECT RAISE(ABORT,'independent correction history is immutable'); END;
CREATE TRIGGER application_correction_receipt_facts_no_collision BEFORE INSERT ON application_correction_receipt_facts WHEN EXISTS(SELECT 1 FROM application_correction_receipt_facts WHERE fact_id=NEW.fact_id OR correction_id=NEW.correction_id OR snapshot_id=NEW.snapshot_id) BEGIN SELECT RAISE(ABORT,'independent correction identity collision'); END;
