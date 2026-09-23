PRAGMA foreign_keys = ON;

-- Append-only controlled correction history. The original transaction remains
-- the sole economic event; these records describe its verified effective view.
CREATE TABLE correction_targets (
    target_id TEXT PRIMARY KEY REFERENCES transactions(public_id),
    route TEXT NOT NULL CHECK (route IN ('text', 'receipt')),
    actor TEXT NOT NULL CHECK (length(trim(actor)) > 0),
    realm TEXT NOT NULL CHECK (length(trim(realm)) > 0),
    key_id TEXT NOT NULL CHECK (length(key_id) = 64 AND key_id NOT GLOB '*[^0-9a-f]*'),
    source_json TEXT NOT NULL CHECK (json_valid(source_json) = 1),
    source_hash TEXT NOT NULL CHECK (length(source_hash) = 64 AND source_hash NOT GLOB '*[^0-9a-f]*'),
    original_hash TEXT NOT NULL CHECK (length(original_hash) = 64 AND original_hash NOT GLOB '*[^0-9a-f]*'),
    projection_json TEXT NOT NULL CHECK (json_valid(projection_json) = 1),
    projection_hash TEXT NOT NULL CHECK (length(projection_hash) = 64 AND projection_hash NOT GLOB '*[^0-9a-f]*'),
    receipt_id TEXT,
    fact_set_id TEXT,
    original_snapshot_id TEXT REFERENCES authoritative_calculation_snapshots(snapshot_public_id),
    original_snapshot_hash TEXT,
    created_at_epoch INTEGER NOT NULL CHECK (created_at_epoch > 0 AND created_at_epoch <= 253402300799),
    UNIQUE (target_id, route),
    CHECK ((route = 'text' AND receipt_id IS NULL AND fact_set_id IS NULL AND original_snapshot_id IS NULL AND original_snapshot_hash IS NULL)
        OR (route = 'receipt' AND receipt_id IS NOT NULL AND fact_set_id IS NOT NULL AND original_snapshot_id IS NOT NULL AND original_snapshot_hash IS NOT NULL))
) STRICT;

CREATE TABLE correction_plans (
    plan_id TEXT PRIMARY KEY,
    target_id TEXT NOT NULL REFERENCES correction_targets(target_id),
    correction_id TEXT NOT NULL UNIQUE,
    authority_id TEXT NOT NULL UNIQUE,
    route TEXT NOT NULL CHECK (route IN ('text', 'receipt')),
    expected_version INTEGER NOT NULL CHECK (expected_version >= 0),
    predecessor_id TEXT,
    predecessor_hash TEXT NOT NULL CHECK (length(predecessor_hash) = 64 AND predecessor_hash NOT GLOB '*[^0-9a-f]*'),
    before_json TEXT NOT NULL CHECK (json_valid(before_json) = 1),
    before_hash TEXT NOT NULL CHECK (length(before_hash) = 64 AND before_hash NOT GLOB '*[^0-9a-f]*'),
    after_json TEXT NOT NULL CHECK (json_valid(after_json) = 1),
    after_hash TEXT NOT NULL CHECK (length(after_hash) = 64 AND after_hash NOT GLOB '*[^0-9a-f]*'),
    source_hash TEXT NOT NULL CHECK (length(source_hash) = 64 AND source_hash NOT GLOB '*[^0-9a-f]*'),
    reason TEXT NOT NULL CHECK (length(trim(reason)) > 0),
    actor TEXT NOT NULL CHECK (length(trim(actor)) > 0),
    realm TEXT NOT NULL CHECK (length(trim(realm)) > 0),
    key_id TEXT NOT NULL CHECK (length(key_id) = 64 AND key_id NOT GLOB '*[^0-9a-f]*'),
    instance_id TEXT NOT NULL CHECK (length(trim(instance_id)) > 0),
    fact_id TEXT UNIQUE,
    fact_hash TEXT,
    snapshot_id TEXT UNIQUE,
    snapshot_hash TEXT,
    receipt_json TEXT CHECK (receipt_json IS NULL OR json_valid(receipt_json) = 1),
    created_at_epoch INTEGER NOT NULL CHECK (created_at_epoch > 0 AND created_at_epoch <= 253402300799),
    expires_at_epoch INTEGER NOT NULL CHECK (expires_at_epoch > created_at_epoch AND expires_at_epoch <= created_at_epoch + 600),
    plan_hash TEXT NOT NULL UNIQUE CHECK (length(plan_hash) = 64 AND plan_hash NOT GLOB '*[^0-9a-f]*'),
    UNIQUE (plan_id, target_id, correction_id, authority_id),
    FOREIGN KEY (target_id, route) REFERENCES correction_targets(target_id, route),
    CHECK ((expected_version = 0 AND predecessor_id IS NULL) OR (expected_version > 0 AND predecessor_id IS NOT NULL)),
    CHECK ((route = 'text' AND fact_id IS NULL AND fact_hash IS NULL AND snapshot_id IS NULL AND snapshot_hash IS NULL AND receipt_json IS NULL)
        OR (route = 'receipt' AND fact_id IS NOT NULL AND fact_hash IS NOT NULL AND snapshot_id IS NOT NULL AND snapshot_hash IS NOT NULL AND receipt_json IS NOT NULL))
) STRICT;

CREATE TABLE correction_versions (
    correction_id TEXT PRIMARY KEY,
    target_id TEXT NOT NULL REFERENCES correction_targets(target_id),
    version INTEGER NOT NULL CHECK (version > 0),
    predecessor_version INTEGER,
    predecessor_id TEXT,
    predecessor_hash TEXT NOT NULL CHECK (length(predecessor_hash) = 64 AND predecessor_hash NOT GLOB '*[^0-9a-f]*'),
    plan_id TEXT NOT NULL UNIQUE,
    authority_id TEXT NOT NULL UNIQUE,
    route TEXT NOT NULL CHECK (route IN ('text', 'receipt')),
    source_hash TEXT NOT NULL,
    before_json TEXT NOT NULL CHECK (json_valid(before_json) = 1),
    before_hash TEXT NOT NULL,
    after_json TEXT NOT NULL CHECK (json_valid(after_json) = 1),
    after_hash TEXT NOT NULL,
    result_core_json TEXT NOT NULL CHECK (json_valid(result_core_json) = 1),
    result_hash TEXT NOT NULL UNIQUE CHECK (length(result_hash) = 64 AND result_hash NOT GLOB '*[^0-9a-f]*'),
    reason TEXT NOT NULL CHECK (length(trim(reason)) > 0),
    actor TEXT NOT NULL CHECK (length(trim(actor)) > 0),
    apply_epoch INTEGER NOT NULL CHECK (apply_epoch > 0 AND apply_epoch <= 253402300799),
    apply_utc TEXT NOT NULL,
    instance_id TEXT NOT NULL,
    fact_id TEXT,
    fact_hash TEXT,
    snapshot_id TEXT,
    snapshot_hash TEXT,
    snapshot_created_at TEXT,
    UNIQUE (target_id, version),
    UNIQUE (target_id, version, correction_id, result_hash),
    UNIQUE (correction_id, plan_id, authority_id, target_id, result_hash),
    UNIQUE (authority_id, plan_id, correction_id, target_id, result_hash),
    UNIQUE (correction_id, target_id, route, fact_id, snapshot_id),
    FOREIGN KEY (plan_id, target_id, correction_id, authority_id)
        REFERENCES correction_plans(plan_id, target_id, correction_id, authority_id),
    FOREIGN KEY (target_id, predecessor_version, predecessor_id, predecessor_hash)
        REFERENCES correction_versions(target_id, version, correction_id, result_hash),
    FOREIGN KEY (authority_id, plan_id, correction_id, target_id, result_hash)
        REFERENCES correction_authorities(authority_id, plan_id, correction_id, target_id, result_hash) DEFERRABLE INITIALLY DEFERRED,
    FOREIGN KEY (fact_id, correction_id, target_id, snapshot_id)
        REFERENCES correction_receipt_facts(fact_id, correction_id, target_id, snapshot_id) DEFERRABLE INITIALLY DEFERRED,
    FOREIGN KEY (target_id, route) REFERENCES correction_targets(target_id, route),
    CHECK ((version = 1 AND predecessor_version IS NULL AND predecessor_id IS NULL)
        OR (version > 1 AND predecessor_version = version - 1 AND predecessor_id IS NOT NULL)),
    CHECK ((route = 'text' AND fact_id IS NULL AND fact_hash IS NULL AND snapshot_id IS NULL AND snapshot_hash IS NULL AND snapshot_created_at IS NULL)
        OR (route = 'receipt' AND fact_id IS NOT NULL AND fact_hash IS NOT NULL AND snapshot_id IS NOT NULL AND snapshot_hash IS NOT NULL AND snapshot_created_at IS NOT NULL))
) STRICT;

CREATE TABLE correction_authorities (
    authority_id TEXT PRIMARY KEY,
    plan_id TEXT NOT NULL UNIQUE,
    correction_id TEXT NOT NULL UNIQUE,
    target_id TEXT NOT NULL,
    result_hash TEXT NOT NULL,
    nonce TEXT NOT NULL UNIQUE CHECK (length(nonce) = 64 AND nonce NOT GLOB '*[^0-9a-f]*'),
    decision_json TEXT NOT NULL CHECK (json_valid(decision_json) = 1),
    decision_signature TEXT NOT NULL CHECK (length(decision_signature) = 64 AND decision_signature NOT GLOB '*[^0-9a-f]*'),
    decision_digest TEXT NOT NULL UNIQUE CHECK (length(decision_digest) = 64 AND decision_digest NOT GLOB '*[^0-9a-f]*'),
    issued_at_epoch INTEGER NOT NULL CHECK (issued_at_epoch > 0),
    expires_at_epoch INTEGER NOT NULL CHECK (expires_at_epoch >= issued_at_epoch),
    checked_at_epoch INTEGER NOT NULL CHECK (checked_at_epoch BETWEEN issued_at_epoch AND expires_at_epoch),
    instance_id TEXT NOT NULL,
    consumption_json TEXT NOT NULL CHECK (json_valid(consumption_json) = 1),
    consumption_seal TEXT NOT NULL CHECK (length(consumption_seal) = 64 AND consumption_seal NOT GLOB '*[^0-9a-f]*'),
    UNIQUE (authority_id, plan_id, correction_id, target_id, result_hash),
    UNIQUE (correction_id, plan_id, authority_id, target_id, result_hash),
    FOREIGN KEY (correction_id, plan_id, authority_id, target_id, result_hash)
        REFERENCES correction_versions(correction_id, plan_id, authority_id, target_id, result_hash) DEFERRABLE INITIALLY DEFERRED,
    FOREIGN KEY (plan_id, target_id, correction_id, authority_id)
        REFERENCES correction_plans(plan_id, target_id, correction_id, authority_id)
) STRICT;

CREATE TABLE correction_receipt_facts (
    fact_id TEXT PRIMARY KEY,
    correction_id TEXT NOT NULL UNIQUE,
    target_id TEXT NOT NULL,
    route TEXT NOT NULL DEFAULT 'receipt' CHECK (route = 'receipt'),
    snapshot_id TEXT NOT NULL UNIQUE REFERENCES authoritative_calculation_snapshots(snapshot_public_id),
    fact_hash TEXT NOT NULL CHECK (length(fact_hash) = 64 AND fact_hash NOT GLOB '*[^0-9a-f]*'),
    fact_json TEXT NOT NULL CHECK (json_valid(fact_json) = 1),
    frozen_self_json TEXT NOT NULL CHECK (json_valid(frozen_self_json) = 1),
    original_receipt_id TEXT NOT NULL,
    original_fact_set_id TEXT NOT NULL,
    original_snapshot_id TEXT NOT NULL,
    previous_snapshot_id TEXT NOT NULL REFERENCES authoritative_calculation_snapshots(snapshot_public_id),
    previous_snapshot_hash TEXT NOT NULL,
    snapshot_hash TEXT NOT NULL,
    snapshot_created_at TEXT NOT NULL,
    UNIQUE (fact_id, correction_id, target_id, snapshot_id),
    FOREIGN KEY (correction_id, target_id, route, fact_id, snapshot_id)
        REFERENCES correction_versions(correction_id, target_id, route, fact_id, snapshot_id) DEFERRABLE INITIALLY DEFERRED
) STRICT;

CREATE TRIGGER trg_correction_receipt_snapshot_match BEFORE INSERT ON correction_receipt_facts
BEGIN
    SELECT RAISE(ABORT, 'correction snapshot binding mismatch')
    WHERE NOT EXISTS (
        SELECT 1 FROM authoritative_calculation_snapshots s
        JOIN correction_plans p ON p.correction_id = NEW.correction_id
        WHERE s.snapshot_public_id = NEW.snapshot_id
          AND s.combined_snapshot_hash = NEW.snapshot_hash
          AND s.created_at = NEW.snapshot_created_at
          AND s.authorization_reference = p.authority_id
          AND s.previous_snapshot_public_id = NEW.previous_snapshot_id
          AND p.snapshot_hash = NEW.snapshot_hash
          AND p.fact_hash = NEW.fact_hash
    );
END;

-- Financial commit can precede D2 coordination catch-up. Protect its audited
-- transaction before SQLite's REPLACE handling can remove the original row.
CREATE TRIGGER trg_correction_transactions_no_update BEFORE UPDATE ON transactions
WHEN (
    EXISTS (SELECT 1 FROM d2_posting_attempts a WHERE a.transaction_public_id = OLD.public_id AND a.stage = 'finalized')
    OR EXISTS (SELECT 1 FROM parser_proposal_conversion_audit c WHERE c.transaction_id = OLD.id)
    OR EXISTS (SELECT 1 FROM receipt_finalization_audit r WHERE r.transaction_public_id = OLD.public_id AND r.status IN ('finalized', 'already_finalized'))
  ) OR EXISTS (
    SELECT 1 FROM transactions t
    WHERE t.id <> OLD.id AND (t.id = NEW.id OR t.public_id = NEW.public_id)
      AND (
          EXISTS (SELECT 1 FROM d2_posting_attempts a WHERE a.transaction_public_id = t.public_id AND a.stage = 'finalized')
          OR EXISTS (SELECT 1 FROM parser_proposal_conversion_audit c WHERE c.transaction_id = t.id)
          OR EXISTS (SELECT 1 FROM receipt_finalization_audit r WHERE r.transaction_public_id = t.public_id AND r.status IN ('finalized', 'already_finalized'))
      )
  )
BEGIN SELECT RAISE(ABORT, 'finalized D2 transaction is immutable or identity collision'); END;
CREATE TRIGGER trg_correction_transactions_no_delete BEFORE DELETE ON transactions
WHEN (
    EXISTS (SELECT 1 FROM d2_posting_attempts a WHERE a.transaction_public_id = OLD.public_id AND a.stage = 'finalized')
    OR EXISTS (SELECT 1 FROM parser_proposal_conversion_audit c WHERE c.transaction_id = OLD.id)
    OR EXISTS (SELECT 1 FROM receipt_finalization_audit r WHERE r.transaction_public_id = OLD.public_id AND r.status IN ('finalized', 'already_finalized'))
)
BEGIN SELECT RAISE(ABORT, 'finalized D2 transaction is immutable'); END;
CREATE TRIGGER trg_correction_transactions_no_insert_collision BEFORE INSERT ON transactions
WHEN EXISTS (
    SELECT 1 FROM transactions t
    WHERE (t.id = NEW.id OR t.public_id = NEW.public_id)
      AND (
          EXISTS (SELECT 1 FROM d2_posting_attempts a WHERE a.transaction_public_id = t.public_id AND a.stage = 'finalized')
          OR EXISTS (SELECT 1 FROM parser_proposal_conversion_audit c WHERE c.transaction_id = t.id)
          OR EXISTS (SELECT 1 FROM receipt_finalization_audit r WHERE r.transaction_public_id = t.public_id AND r.status IN ('finalized', 'already_finalized'))
      )
)
BEGIN SELECT RAISE(ABORT, 'finalized D2 transaction identity collision'); END;

-- Review resolution may advance status, but must not rewrite the candidate or
-- evidence that the human decision was based on. Explicit collision checks run
-- before SQLite can turn OR REPLACE into a DELETE or OR IGNORE into a no-op.
CREATE TRIGGER trg_correction_review_queue_source_no_update BEFORE UPDATE ON reconciliation_review_queue
WHEN OLD.id IS NOT NEW.id
  OR OLD.public_id IS NOT NEW.public_id
  OR OLD.run_public_id IS NOT NEW.run_public_id
  OR OLD.candidate_id IS NOT NEW.candidate_id
  OR OLD.issue_type IS NOT NEW.issue_type
  OR OLD.suggested_action IS NOT NEW.suggested_action
  OR OLD.priority IS NOT NEW.priority
  OR OLD.statement_transaction_ref IS NOT NEW.statement_transaction_ref
  OR OLD.app_transaction_ref IS NOT NEW.app_transaction_ref
  OR OLD.confidence_score IS NOT NEW.confidence_score
  OR OLD.reason_codes_json IS NOT NEW.reason_codes_json
  OR OLD.evidence_json IS NOT NEW.evidence_json
  OR OLD.created_at IS NOT NEW.created_at
BEGIN SELECT RAISE(ABORT, 'review queue source fields are immutable'); END;
CREATE TRIGGER trg_correction_review_queue_no_delete BEFORE DELETE ON reconciliation_review_queue
BEGIN SELECT RAISE(ABORT, 'review queue source rows are immutable'); END;
CREATE TRIGGER trg_correction_review_queue_no_insert_collision BEFORE INSERT ON reconciliation_review_queue
WHEN EXISTS (
    SELECT 1 FROM reconciliation_review_queue q
    WHERE q.id = NEW.id OR q.public_id = NEW.public_id
)
BEGIN SELECT RAISE(ABORT, 'review queue identity collision'); END;

-- Resolution and final-mutation success are part of the same frozen
-- relationship evidence as the queue source. Protect every stored outcome,
-- including failures: changing success 1 -> 0 or deleting a row must never
-- make a previously committed relationship disappear from history. Explicit
-- collisions also precede SQLite's OR REPLACE / OR IGNORE handling.
CREATE TRIGGER trg_correction_resolution_decisions_no_update BEFORE UPDATE ON reconciliation_resolution_decisions BEGIN SELECT RAISE(ABORT, 'resolution decisions are append-only'); END;
CREATE TRIGGER trg_correction_resolution_decisions_no_delete BEFORE DELETE ON reconciliation_resolution_decisions BEGIN SELECT RAISE(ABORT, 'resolution decisions are append-only'); END;
CREATE TRIGGER trg_correction_resolution_decisions_no_insert_collision BEFORE INSERT ON reconciliation_resolution_decisions WHEN EXISTS (SELECT 1 FROM reconciliation_resolution_decisions WHERE id = NEW.id OR public_id = NEW.public_id) BEGIN SELECT RAISE(ABORT, 'resolution decision identity collision'); END;
CREATE TRIGGER trg_correction_resolution_results_no_update BEFORE UPDATE ON reconciliation_resolution_results BEGIN SELECT RAISE(ABORT, 'resolution results are append-only'); END;
CREATE TRIGGER trg_correction_resolution_results_no_delete BEFORE DELETE ON reconciliation_resolution_results BEGIN SELECT RAISE(ABORT, 'resolution results are append-only'); END;
CREATE TRIGGER trg_correction_resolution_results_no_insert_collision BEFORE INSERT ON reconciliation_resolution_results WHEN EXISTS (SELECT 1 FROM reconciliation_resolution_results WHERE id = NEW.id OR public_id = NEW.public_id) BEGIN SELECT RAISE(ABORT, 'resolution result identity collision'); END;
CREATE TRIGGER trg_correction_apply_results_no_update BEFORE UPDATE ON reconciliation_apply_results BEGIN SELECT RAISE(ABORT, 'apply results are append-only'); END;
CREATE TRIGGER trg_correction_apply_results_no_delete BEFORE DELETE ON reconciliation_apply_results BEGIN SELECT RAISE(ABORT, 'apply results are append-only'); END;
CREATE TRIGGER trg_correction_apply_results_no_insert_collision BEFORE INSERT ON reconciliation_apply_results WHEN EXISTS (SELECT 1 FROM reconciliation_apply_results WHERE id = NEW.id OR apply_id = NEW.apply_id OR decision_id = NEW.decision_id) BEGIN SELECT RAISE(ABORT, 'apply result identity collision'); END;
CREATE TRIGGER trg_correction_final_mutation_audit_no_update BEFORE UPDATE ON reconciliation_final_mutation_audit BEGIN SELECT RAISE(ABORT, 'final mutation audit is append-only'); END;
CREATE TRIGGER trg_correction_final_mutation_audit_no_delete BEFORE DELETE ON reconciliation_final_mutation_audit BEGIN SELECT RAISE(ABORT, 'final mutation audit is append-only'); END;
CREATE TRIGGER trg_correction_final_mutation_audit_no_insert_collision BEFORE INSERT ON reconciliation_final_mutation_audit WHEN EXISTS (SELECT 1 FROM reconciliation_final_mutation_audit WHERE rowid = NEW.rowid OR final_mutation_id = NEW.final_mutation_id OR idempotency_key = NEW.idempotency_key) BEGIN SELECT RAISE(ABORT, 'final mutation audit identity collision'); END;

CREATE TRIGGER trg_correction_targets_no_update BEFORE UPDATE ON correction_targets BEGIN SELECT RAISE(ABORT, 'correction targets are append-only'); END;
CREATE TRIGGER trg_correction_targets_no_delete BEFORE DELETE ON correction_targets BEGIN SELECT RAISE(ABORT, 'correction targets are append-only'); END;
CREATE TRIGGER trg_correction_targets_no_insert_collision BEFORE INSERT ON correction_targets WHEN EXISTS (SELECT 1 FROM correction_targets WHERE target_id = NEW.target_id) BEGIN SELECT RAISE(ABORT, 'correction target identity collision'); END;
CREATE TRIGGER trg_correction_plans_no_update BEFORE UPDATE ON correction_plans BEGIN SELECT RAISE(ABORT, 'correction plans are append-only'); END;
CREATE TRIGGER trg_correction_plans_no_delete BEFORE DELETE ON correction_plans BEGIN SELECT RAISE(ABORT, 'correction plans are append-only'); END;
CREATE TRIGGER trg_correction_plans_no_insert_collision BEFORE INSERT ON correction_plans WHEN EXISTS (SELECT 1 FROM correction_plans WHERE plan_id = NEW.plan_id OR correction_id = NEW.correction_id OR authority_id = NEW.authority_id) BEGIN SELECT RAISE(ABORT, 'correction plan identity collision'); END;
CREATE TRIGGER trg_correction_versions_no_update BEFORE UPDATE ON correction_versions BEGIN SELECT RAISE(ABORT, 'correction versions are append-only'); END;
CREATE TRIGGER trg_correction_versions_no_delete BEFORE DELETE ON correction_versions BEGIN SELECT RAISE(ABORT, 'correction versions are append-only'); END;
CREATE TRIGGER trg_correction_versions_no_insert_collision BEFORE INSERT ON correction_versions WHEN EXISTS (SELECT 1 FROM correction_versions WHERE correction_id = NEW.correction_id OR (target_id = NEW.target_id AND version = NEW.version) OR plan_id = NEW.plan_id OR authority_id = NEW.authority_id OR result_hash = NEW.result_hash) BEGIN SELECT RAISE(ABORT, 'correction version identity collision'); END;
CREATE TRIGGER trg_correction_authorities_no_update BEFORE UPDATE ON correction_authorities BEGIN SELECT RAISE(ABORT, 'correction authorities are append-only'); END;
CREATE TRIGGER trg_correction_authorities_no_delete BEFORE DELETE ON correction_authorities BEGIN SELECT RAISE(ABORT, 'correction authorities are append-only'); END;
CREATE TRIGGER trg_correction_authorities_no_insert_collision BEFORE INSERT ON correction_authorities WHEN EXISTS (SELECT 1 FROM correction_authorities WHERE authority_id = NEW.authority_id OR plan_id = NEW.plan_id OR correction_id = NEW.correction_id OR nonce = NEW.nonce OR decision_digest = NEW.decision_digest) BEGIN SELECT RAISE(ABORT, 'correction authority identity collision'); END;
CREATE TRIGGER trg_correction_receipt_facts_no_update BEFORE UPDATE ON correction_receipt_facts BEGIN SELECT RAISE(ABORT, 'correction receipt facts are append-only'); END;
CREATE TRIGGER trg_correction_receipt_facts_no_delete BEFORE DELETE ON correction_receipt_facts BEGIN SELECT RAISE(ABORT, 'correction receipt facts are append-only'); END;
CREATE TRIGGER trg_correction_receipt_facts_no_insert_collision BEFORE INSERT ON correction_receipt_facts WHEN EXISTS (SELECT 1 FROM correction_receipt_facts WHERE fact_id = NEW.fact_id OR correction_id = NEW.correction_id OR snapshot_id = NEW.snapshot_id) BEGIN SELECT RAISE(ABORT, 'correction receipt fact identity collision'); END;

PRAGMA foreign_keys = ON;
