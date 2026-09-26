PRAGMA foreign_keys = ON;

-- One immutable interpretation of each adopted Telegram message.  Financial
-- actions are deliberately absent: a route is work to recover, not authority.
CREATE TABLE finance_capture_interaction_routes (
    job_public_id TEXT PRIMARY KEY REFERENCES finance_capture_jobs(public_id),
    route_kind TEXT NOT NULL CHECK (route_kind IN
        ('initial_intake', 'whole_card', 'guided_update', 'guided_complete', 'control_refused')),
    raw_text_sha256 TEXT NOT NULL CHECK (length(raw_text_sha256) = 64
        AND raw_text_sha256 NOT GLOB '*[^0-9a-f]*'),
    authenticated_actor_id TEXT NOT NULL,
    telegram_account_id TEXT NOT NULL,
    telegram_conversation_id TEXT NOT NULL,
    conversation_binding_id TEXT NOT NULL,
    telegram_message_id INTEGER NOT NULL CHECK (telegram_message_id > 0),
    card_generation_public_id TEXT,
    guided_session_public_id TEXT,
    operation_key TEXT,
    field_name TEXT,
    field_value_json TEXT CHECK (field_value_json IS NULL OR json_valid(field_value_json) = 1),
    refusal_code TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (telegram_account_id, telegram_conversation_id,
        conversation_binding_id, telegram_message_id),
    CHECK ((route_kind = 'whole_card' AND card_generation_public_id IS NOT NULL
            AND operation_key IS NOT NULL)
        OR (route_kind IN ('guided_update', 'guided_complete')
            AND guided_session_public_id IS NOT NULL AND operation_key IS NOT NULL)
        OR (route_kind = 'initial_intake'
            AND card_generation_public_id IS NULL
            AND guided_session_public_id IS NULL AND operation_key IS NULL)
        OR route_kind = 'control_refused'),
    CHECK ((route_kind = 'guided_update' AND field_name IS NOT NULL
            AND field_value_json IS NOT NULL)
        OR (route_kind != 'guided_update' AND field_name IS NULL
            AND field_value_json IS NULL)),
    CHECK ((route_kind = 'control_refused' AND refusal_code IS NOT NULL)
        OR (route_kind != 'control_refused' AND refusal_code IS NULL))
) STRICT, WITHOUT ROWID;

CREATE UNIQUE INDEX finance_capture_interaction_routes_operation_idx
    ON finance_capture_interaction_routes(operation_key)
    WHERE operation_key IS NOT NULL;

CREATE TRIGGER trg_finance_capture_interaction_routes_require_source
BEFORE INSERT ON finance_capture_interaction_routes
WHEN NOT EXISTS (
    SELECT 1 FROM finance_capture_jobs AS job
    JOIN raw_intake_records AS intake ON intake.id = job.raw_intake_record_id
    JOIN d2_telegram_source_contexts AS source
      ON source.raw_intake_record_id = intake.id
    WHERE job.public_id = NEW.job_public_id AND job.capture_kind = 'text'
      AND intake.source_type = 'telegram_text'
      AND intake.source_channel = 'telegram'
      AND source.authenticated_actor_id = NEW.authenticated_actor_id
      AND source.telegram_account_id = NEW.telegram_account_id
      AND source.telegram_conversation_id = NEW.telegram_conversation_id
      AND source.conversation_binding_id = NEW.conversation_binding_id
      AND source.source_message_id = CAST(NEW.telegram_message_id AS TEXT)
)
BEGIN
    SELECT RAISE(ABORT, 'interaction route source linkage mismatch');
END;

CREATE TRIGGER trg_finance_capture_interaction_routes_no_update
BEFORE UPDATE ON finance_capture_interaction_routes
BEGIN
    SELECT RAISE(ABORT, 'interaction route is immutable');
END;

CREATE TRIGGER trg_finance_capture_interaction_routes_no_delete
BEFORE DELETE ON finance_capture_interaction_routes
BEGIN
    SELECT RAISE(ABORT, 'interaction route cannot be deleted');
END;

CREATE TRIGGER trg_finance_capture_interaction_routes_no_replace
BEFORE INSERT ON finance_capture_interaction_routes
WHEN EXISTS (SELECT 1 FROM finance_capture_interaction_routes AS prior
    WHERE prior.job_public_id = NEW.job_public_id
       OR (NEW.operation_key IS NOT NULL
           AND prior.operation_key = NEW.operation_key)
       OR (prior.telegram_account_id = NEW.telegram_account_id
           AND prior.telegram_conversation_id = NEW.telegram_conversation_id
           AND prior.conversation_binding_id = NEW.conversation_binding_id
           AND prior.telegram_message_id = NEW.telegram_message_id))
BEGIN
    SELECT RAISE(ABORT, 'interaction route collision cannot replace evidence');
END;

-- The cutover must not strand a Telegram text proposal that was already
-- bound to its raw source before this migration.  Snapshot only established,
-- matching source lineage; an old unparsed intake receives no admission.
CREATE TABLE finance_legacy_text_lineage_admissions (
    raw_intake_record_id INTEGER PRIMARY KEY REFERENCES raw_intake_records(id),
    source_public_id TEXT NOT NULL UNIQUE,
    admitted_parser_output_id INTEGER NOT NULL UNIQUE REFERENCES parser_outputs(id)
) STRICT, WITHOUT ROWID;

INSERT INTO finance_legacy_text_lineage_admissions (
    raw_intake_record_id, source_public_id, admitted_parser_output_id
)
SELECT intake.id, intake.public_id, parent.id
FROM raw_intake_records AS intake
JOIN parser_outputs AS parent ON parent.id = intake.parser_output_id
    AND parent.source_public_id = intake.public_id
    AND parent.source_type = 'telegram_text'
WHERE intake.source_type = 'telegram_text'
  AND intake.source_channel = 'telegram'
  AND (SELECT COUNT(*) FROM raw_intake_records AS other
       WHERE other.parser_output_id = parent.id) = 1;

CREATE TRIGGER trg_finance_legacy_text_lineage_no_insert
BEFORE INSERT ON finance_legacy_text_lineage_admissions
BEGIN
    SELECT RAISE(ABORT, 'legacy text admission is migration-only');
END;

CREATE TRIGGER trg_finance_legacy_text_lineage_no_update
BEFORE UPDATE ON finance_legacy_text_lineage_admissions
BEGIN
    SELECT RAISE(ABORT, 'legacy text admission is immutable');
END;

CREATE TRIGGER trg_finance_legacy_text_lineage_no_delete
BEFORE DELETE ON finance_legacy_text_lineage_admissions
BEGIN
    SELECT RAISE(ABORT, 'legacy text admission cannot be deleted');
END;

-- A worker or older API must not reinterpret adopted edit/control source as
-- a fresh parser proposal or a new AI fallback attempt. An authenticated
-- ingress job without a route is quarantined rather than assumed ordinary.
-- Existing admitted lineage may add only a direct child of its current
-- proposal; it cannot use the admission to start a new unrelated proposal.
CREATE TRIGGER trg_finance_interaction_no_control_parser
BEFORE INSERT ON parser_outputs
WHEN EXISTS (
    SELECT 1 FROM raw_intake_records AS intake
    LEFT JOIN finance_capture_jobs AS job ON job.raw_intake_record_id = intake.id
    LEFT JOIN finance_capture_interaction_routes AS route
      ON route.job_public_id = job.public_id
    WHERE intake.public_id = NEW.source_public_id
      AND intake.source_type = 'telegram_text'
      AND intake.source_channel = 'telegram'
      AND (job.public_id IS NULL OR job.capture_kind != 'text'
           OR route.route_kind IS NULL OR route.route_kind != 'initial_intake')
      AND NOT EXISTS (
          SELECT 1 FROM finance_legacy_text_lineage_admissions AS legacy
          WHERE legacy.raw_intake_record_id = intake.id
            AND legacy.source_public_id = intake.public_id
            AND NEW.source_type = 'telegram_text'
            AND NEW.parent_parser_output_id = intake.parser_output_id
      )
)
BEGIN
    SELECT RAISE(ABORT, 'Telegram text requires initial intake route before parser');
END;

CREATE TRIGGER trg_finance_interaction_no_control_ai
BEFORE INSERT ON ai_fallback_attempts
WHEN EXISTS (
    SELECT 1 FROM raw_intake_records AS intake
    LEFT JOIN finance_capture_jobs AS job ON job.raw_intake_record_id = intake.id
    LEFT JOIN finance_capture_interaction_routes AS route
      ON route.job_public_id = job.public_id
    WHERE intake.id = NEW.raw_intake_record_id
      AND intake.source_type = 'telegram_text'
      AND intake.source_channel = 'telegram'
      AND (job.public_id IS NULL OR job.capture_kind != 'text'
           OR route.route_kind IS NULL OR route.route_kind != 'initial_intake')
      AND NOT EXISTS (
          SELECT 1 FROM finance_legacy_text_lineage_admissions AS legacy
          WHERE legacy.raw_intake_record_id = intake.id
            AND legacy.source_public_id = intake.public_id
            AND NEW.parent_parser_output_id = intake.parser_output_id
      )
)
BEGIN
    SELECT RAISE(ABORT, 'Telegram text requires initial intake route before AI fallback');
END;
