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
    d1_compatibility_operation_public_id TEXT,
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
            AND field_value_json IS NOT NULL
            AND d1_compatibility_operation_public_id IS NOT NULL)
        OR (route_kind != 'guided_update' AND field_name IS NULL
            AND field_value_json IS NULL
            AND d1_compatibility_operation_public_id IS NULL)),
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

-- Admit only the exact guided update already pending at cutover.  A timestamp
-- or an update_requested event created after cutover is not proof of history.
CREATE TABLE finance_legacy_guided_pending_admissions (
    session_id INTEGER PRIMARY KEY REFERENCES openclaw_guided_edit_sessions(id),
    session_public_id TEXT NOT NULL UNIQUE,
    request_event_id INTEGER NOT NULL UNIQUE REFERENCES openclaw_guided_edit_events(id),
    telegram_message_id INTEGER NOT NULL,
    operation_key TEXT NOT NULL,
    field_name TEXT NOT NULL,
    field_value_json TEXT NOT NULL,
    authenticated_actor_id TEXT NOT NULL,
    channel_account_id TEXT NOT NULL,
    channel_conversation_id TEXT NOT NULL,
    conversation_binding_id TEXT NOT NULL
) STRICT, WITHOUT ROWID;

INSERT INTO finance_legacy_guided_pending_admissions (
    session_id, session_public_id, request_event_id, telegram_message_id,
    operation_key, field_name, field_value_json, authenticated_actor_id,
    channel_account_id, channel_conversation_id, conversation_binding_id
)
SELECT session.id, session.session_public_id, event.id,
       session.pending_message_id, session.pending_operation_key,
       session.pending_field_name, session.pending_field_value_json,
       session.authenticated_actor_id, session.channel_account_id,
       session.channel_conversation_id, session.conversation_binding_id
FROM openclaw_guided_edit_sessions AS session
JOIN openclaw_guided_edit_events AS event
  ON event.session_id = session.id
 AND event.event_type = 'update_requested'
 AND event.telegram_message_id = session.pending_message_id
 AND event.operation_key = session.pending_operation_key
 AND event.field_name = session.pending_field_name
 AND event.field_value_json = session.pending_field_value_json
 AND event.before_parser_output_id = session.current_parser_output_id
 AND event.before_proposal_version = session.current_proposal_version
 AND event.before_content_hash = session.current_content_hash
WHERE session.status = 'active' AND session.pending_message_id IS NOT NULL
  AND (SELECT COUNT(*) FROM openclaw_guided_edit_events AS matching
       WHERE matching.session_id = session.id
         AND matching.telegram_message_id = session.pending_message_id
         AND matching.event_type = 'update_requested') = 1;

CREATE TRIGGER trg_finance_legacy_guided_pending_no_insert
BEFORE INSERT ON finance_legacy_guided_pending_admissions
BEGIN
    SELECT RAISE(ABORT, 'legacy guided admission is migration-only');
END;

CREATE TRIGGER trg_finance_legacy_guided_pending_no_update
BEFORE UPDATE ON finance_legacy_guided_pending_admissions
BEGIN
    SELECT RAISE(ABORT, 'legacy guided admission is immutable');
END;

CREATE TRIGGER trg_finance_legacy_guided_pending_no_delete
BEFORE DELETE ON finance_legacy_guided_pending_admissions
BEGIN
    SELECT RAISE(ABORT, 'legacy guided admission cannot be deleted');
END;

CREATE TRIGGER trg_finance_guided_new_session_no_pending
BEFORE INSERT ON openclaw_guided_edit_sessions
WHEN NEW.pending_message_id IS NOT NULL
BEGIN
    SELECT RAISE(ABORT, 'new guided pending update requires a frozen route');
END;

CREATE TRIGGER trg_finance_guided_pending_requires_route
BEFORE UPDATE ON openclaw_guided_edit_sessions
WHEN NEW.pending_message_id IS NOT NULL
 AND (OLD.pending_message_id IS NOT NEW.pending_message_id
      OR OLD.pending_operation_key IS NOT NEW.pending_operation_key
      OR OLD.pending_field_name IS NOT NEW.pending_field_name
      OR OLD.pending_field_value_json IS NOT NEW.pending_field_value_json)
 AND NOT EXISTS (
    SELECT 1 FROM finance_capture_interaction_routes AS route
    JOIN parser_outputs AS proposal ON proposal.id = NEW.current_parser_output_id
    WHERE route.route_kind = 'guided_update'
      AND route.guided_session_public_id = NEW.session_public_id
      AND route.telegram_message_id = NEW.pending_message_id
      AND route.authenticated_actor_id = NEW.authenticated_actor_id
      AND route.telegram_account_id = NEW.channel_account_id
      AND route.telegram_conversation_id = NEW.channel_conversation_id
      AND route.conversation_binding_id = NEW.conversation_binding_id
      AND route.field_name = NEW.pending_field_name
      AND NEW.pending_operation_key =
          'bridge-edit:' || proposal.public_id || ':v' ||
          NEW.current_proposal_version || ':' || NEW.current_content_hash
      AND json_extract(route.field_value_json, '$')
          = json_extract(NEW.pending_field_value_json, '$')
 )
BEGIN
    SELECT RAISE(ABORT, 'guided pending update requires a frozen route');
END;

CREATE TRIGGER trg_finance_guided_pending_state_immutable
BEFORE UPDATE ON openclaw_guided_edit_sessions
WHEN OLD.pending_message_id IS NOT NULL AND NEW.pending_message_id IS NOT NULL
 AND (NEW.current_parser_output_id IS NOT OLD.current_parser_output_id
      OR NEW.current_proposal_version IS NOT OLD.current_proposal_version
      OR NEW.current_content_hash IS NOT OLD.current_content_hash)
BEGIN
    SELECT RAISE(ABORT, 'guided pending proposal state is immutable');
END;

CREATE TRIGGER trg_finance_guided_request_requires_route
BEFORE INSERT ON openclaw_guided_edit_events
WHEN NEW.event_type = 'update_requested'
 AND NOT EXISTS (
    SELECT 1 FROM openclaw_guided_edit_sessions AS session
    JOIN finance_capture_interaction_routes AS route
      ON route.guided_session_public_id = session.session_public_id
    WHERE session.id = NEW.session_id
      AND session.status = 'active'
      AND session.pending_message_id = NEW.telegram_message_id
      AND session.pending_operation_key = NEW.operation_key
      AND session.pending_field_name = NEW.field_name
      AND session.pending_field_value_json = NEW.field_value_json
      AND session.current_parser_output_id = NEW.before_parser_output_id
      AND session.current_proposal_version = NEW.before_proposal_version
      AND session.current_content_hash = NEW.before_content_hash
      AND route.route_kind = 'guided_update'
      AND route.telegram_message_id = NEW.telegram_message_id
      AND route.authenticated_actor_id = session.authenticated_actor_id
      AND route.telegram_account_id = session.channel_account_id
      AND route.telegram_conversation_id = session.channel_conversation_id
      AND route.conversation_binding_id = session.conversation_binding_id
      AND route.field_name = NEW.field_name
      AND json_extract(route.field_value_json, '$')
          = json_extract(NEW.field_value_json, '$')
 )
BEGIN
    SELECT RAISE(ABORT, 'guided request event requires a frozen route');
END;

CREATE TRIGGER trg_finance_guided_completion_requires_route
BEFORE UPDATE ON openclaw_guided_edit_sessions
WHEN OLD.status = 'active' AND NEW.status = 'completed'
 AND NOT EXISTS (
    SELECT 1 FROM finance_capture_interaction_routes AS route
    WHERE route.route_kind = 'guided_complete'
      AND route.guided_session_public_id = NEW.session_public_id
      AND route.telegram_message_id = NEW.completed_message_id
      AND route.authenticated_actor_id = NEW.authenticated_actor_id
      AND route.telegram_account_id = NEW.channel_account_id
      AND route.telegram_conversation_id = NEW.channel_conversation_id
      AND route.conversation_binding_id = NEW.conversation_binding_id
 )
BEGIN
    SELECT RAISE(ABORT, 'guided completion requires a frozen route');
END;

CREATE TRIGGER trg_finance_guided_completion_event_requires_route
BEFORE INSERT ON openclaw_guided_edit_events
WHEN NEW.event_type = 'completed'
 AND NOT EXISTS (
    SELECT 1 FROM openclaw_guided_edit_sessions AS session
    JOIN finance_capture_interaction_routes AS route
      ON route.guided_session_public_id = session.session_public_id
    WHERE session.id = NEW.session_id
      AND route.route_kind = 'guided_complete'
      AND route.telegram_message_id = NEW.telegram_message_id
      AND route.authenticated_actor_id = session.authenticated_actor_id
      AND route.telegram_account_id = session.channel_account_id
      AND route.telegram_conversation_id = session.channel_conversation_id
      AND route.conversation_binding_id = session.conversation_binding_id
 )
BEGIN
    SELECT RAISE(ABORT, 'guided completion event requires a frozen route');
END;

-- D1's Telegram reply evidence is another business-write boundary.  The
-- guided compatibility card is generated from the original guided message;
-- its bytes differ from the saved Telegram text.  Keep that exact mapping
-- queryable for both the Core API and direct SQL guards.
CREATE VIEW finance_d3_guided_reply_authority AS
SELECT draft.id AS draft_id,
       session.pending_message_id AS telegram_message_id,
       session.authenticated_actor_id,
       session.channel_account_id AS telegram_account_id,
       session.channel_conversation_id AS telegram_conversation_id,
       session.conversation_binding_id,
       route.d1_compatibility_operation_public_id AS routed_d1_operation_public_id,
       'Card Ref: ' || draft.current_card_generation_public_id || char(10) ||
       CASE session.pending_field_name
           WHEN 'amount' THEN 'Amount'
           WHEN 'currency' THEN 'Currency'
           WHEN 'transaction_date' THEN 'Date'
           WHEN 'merchant' THEN 'Merchant'
           WHEN 'description' THEN 'Description'
           WHEN 'category' THEN 'Category'
       END || ': ' || json_extract(session.pending_field_value_json, '$')
           AS expected_card_text
FROM parser_human_drafts AS draft
JOIN openclaw_guided_edit_sessions AS session
  ON session.source_reference_id = draft.source_edit_reference_id
 AND session.status = 'active' AND session.pending_message_id IS NOT NULL
JOIN openclaw_guided_edit_events AS event
  ON event.session_id = session.id
 AND event.event_type = 'update_requested'
 AND event.telegram_message_id = session.pending_message_id
 AND event.operation_key = session.pending_operation_key
 AND event.field_name = session.pending_field_name
 AND event.field_value_json = session.pending_field_value_json
LEFT JOIN finance_capture_interaction_routes AS route
  ON route.route_kind = 'guided_update'
 AND route.guided_session_public_id = session.session_public_id
 AND route.telegram_message_id = session.pending_message_id
 AND route.authenticated_actor_id = session.authenticated_actor_id
 AND route.telegram_account_id = session.channel_account_id
 AND route.telegram_conversation_id = session.channel_conversation_id
 AND route.conversation_binding_id = session.conversation_binding_id
 AND route.field_name = session.pending_field_name
 AND json_extract(route.field_value_json, '$')
     = json_extract(session.pending_field_value_json, '$')
LEFT JOIN finance_legacy_guided_pending_admissions AS legacy
  ON legacy.session_id = session.id
 AND legacy.session_public_id = session.session_public_id
 AND legacy.request_event_id = event.id
 AND legacy.telegram_message_id = session.pending_message_id
 AND legacy.operation_key = session.pending_operation_key
 AND legacy.field_name = session.pending_field_name
 AND legacy.field_value_json = session.pending_field_value_json
 AND legacy.authenticated_actor_id = session.authenticated_actor_id
 AND legacy.channel_account_id = session.channel_account_id
 AND legacy.channel_conversation_id = session.channel_conversation_id
 AND legacy.conversation_binding_id = session.conversation_binding_id
WHERE route.job_public_id IS NOT NULL OR legacy.session_id IS NOT NULL;

CREATE TRIGGER trg_finance_d1_reply_evidence_requires_route
BEFORE INSERT ON parser_human_draft_reply_evidence
WHEN NOT EXISTS (
    SELECT 1 FROM parser_human_drafts AS draft
    JOIN finance_capture_interaction_routes AS route
      ON route.route_kind = 'whole_card'
     AND route.card_generation_public_id = draft.current_card_generation_public_id
    JOIN finance_capture_jobs AS job ON job.public_id = route.job_public_id
    JOIN raw_intake_records AS intake ON intake.id = job.raw_intake_record_id
    WHERE draft.id = NEW.draft_id
      AND route.authenticated_actor_id = NEW.authenticated_actor_id
      AND route.telegram_account_id = NEW.telegram_account_id
      AND route.telegram_conversation_id = NEW.telegram_conversation_id
      AND route.conversation_binding_id = NEW.conversation_binding_id
      AND route.telegram_message_id = NEW.telegram_message_id
      AND route.raw_text_sha256 = NEW.sha256
      AND CAST(intake.raw_input AS BLOB) = NEW.raw_utf8
 )
 AND NOT EXISTS (
    SELECT 1 FROM finance_d3_guided_reply_authority AS guided
    WHERE guided.draft_id = NEW.draft_id
      AND guided.authenticated_actor_id = NEW.authenticated_actor_id
      AND guided.telegram_account_id = NEW.telegram_account_id
      AND guided.telegram_conversation_id = NEW.telegram_conversation_id
      AND guided.conversation_binding_id = NEW.conversation_binding_id
      AND guided.telegram_message_id = NEW.telegram_message_id
      AND CAST(guided.expected_card_text AS BLOB) = NEW.raw_utf8
 )
BEGIN
    SELECT RAISE(ABORT, 'D1 Telegram reply requires frozen route or cutover admission');
END;

CREATE TRIGGER trg_finance_d1_operation_requires_route
BEFORE INSERT ON parser_human_draft_operations
WHEN NEW.operation_type IN ('accepted', 'refused', 'noop')
 AND NOT EXISTS (
    SELECT 1 FROM parser_human_draft_reply_evidence AS evidence
    JOIN parser_human_drafts AS draft ON draft.id = evidence.draft_id
    JOIN finance_capture_interaction_routes AS route
      ON route.route_kind = 'whole_card'
     AND route.card_generation_public_id = draft.current_card_generation_public_id
     AND route.operation_key = NEW.operation_public_id
    JOIN finance_capture_jobs AS job ON job.public_id = route.job_public_id
    JOIN raw_intake_records AS intake ON intake.id = job.raw_intake_record_id
    WHERE evidence.id = NEW.human_reply_evidence_id
      AND evidence.draft_id = NEW.draft_id
      AND evidence.telegram_message_id = NEW.telegram_message_id
      AND route.authenticated_actor_id = evidence.authenticated_actor_id
      AND route.telegram_account_id = evidence.telegram_account_id
      AND route.telegram_conversation_id = evidence.telegram_conversation_id
      AND route.conversation_binding_id = evidence.conversation_binding_id
      AND route.telegram_message_id = evidence.telegram_message_id
      AND route.raw_text_sha256 = evidence.sha256
      AND CAST(intake.raw_input AS BLOB) = evidence.raw_utf8
 )
 AND NOT EXISTS (
    SELECT 1 FROM parser_human_draft_reply_evidence AS evidence
    JOIN finance_d3_guided_reply_authority AS guided
      ON guided.draft_id = evidence.draft_id
     AND guided.telegram_message_id = evidence.telegram_message_id
    WHERE evidence.id = NEW.human_reply_evidence_id
      AND evidence.draft_id = NEW.draft_id
      AND evidence.telegram_message_id = NEW.telegram_message_id
      AND guided.authenticated_actor_id = evidence.authenticated_actor_id
      AND guided.telegram_account_id = evidence.telegram_account_id
      AND guided.telegram_conversation_id = evidence.telegram_conversation_id
      AND guided.conversation_binding_id = evidence.conversation_binding_id
      AND CAST(guided.expected_card_text AS BLOB) = evidence.raw_utf8
      AND (guided.routed_d1_operation_public_id = NEW.operation_public_id
           OR (guided.routed_d1_operation_public_id IS NULL
               AND NEW.operation_public_id GLOB 'd1op_[0-9a-f]*'))
 )
BEGIN
    SELECT RAISE(ABORT, 'D1 Telegram operation requires frozen route or cutover admission');
END;

-- Source classification cannot be changed to escape the parser gate before a
-- capture job exists.  Image and manual sources retain their own contracts.
CREATE TRIGGER trg_finance_telegram_text_source_identity
BEFORE UPDATE ON raw_intake_records
WHEN (OLD.source_type = 'telegram_text' OR NEW.source_type = 'telegram_text')
 AND (NEW.id IS NOT OLD.id OR NEW.public_id IS NOT OLD.public_id
      OR NEW.source_type IS NOT OLD.source_type
      OR NEW.source_channel IS NOT OLD.source_channel)
BEGIN
    SELECT RAISE(ABORT, 'Telegram text source identity is immutable');
END;

-- A parser may have been inserted before its future source existed.  A new
-- Telegram text raw row cannot adopt that proposal or start with a pointer.
CREATE TRIGGER trg_finance_telegram_text_no_prebound_insert
BEFORE INSERT ON raw_intake_records
WHEN NEW.source_type = 'telegram_text'
 AND (NEW.parser_output_id IS NOT NULL
      OR EXISTS (SELECT 1 FROM parser_outputs AS proposal
                 WHERE proposal.source_public_id = NEW.public_id))
BEGIN
    SELECT RAISE(ABORT, 'Telegram text cannot adopt a prebound parser');
END;

-- A worker or older API must not reinterpret adopted edit/control source as
-- a fresh parser proposal or a new AI fallback attempt. An authenticated
-- ingress job without a route is quarantined rather than assumed ordinary.
-- Existing admitted lineage may add only a direct child of its current
-- proposal; it cannot use the admission to start a new unrelated proposal.
CREATE TRIGGER trg_finance_telegram_text_parser_type
BEFORE INSERT ON parser_outputs
WHEN NEW.source_type != 'telegram_text'
 AND EXISTS (SELECT 1 FROM raw_intake_records AS intake
             WHERE intake.public_id = NEW.source_public_id
               AND intake.source_type = 'telegram_text')
BEGIN
    SELECT RAISE(ABORT, 'Telegram text parser source type mismatch');
END;

-- SQLite REPLACE may silently delete the conflicting target before an ordinary
-- identity trigger can inspect it. Protect both the linked parser id and its
-- public identity, even when the incoming row claims an unrelated source.
-- A distinct new child keeps its own id/public_id and remains admissible.
CREATE TRIGGER trg_finance_telegram_text_parser_no_insert_collision
BEFORE INSERT ON parser_outputs
WHEN EXISTS (
    SELECT 1 FROM parser_outputs AS existing
    JOIN raw_intake_records AS intake
      ON intake.source_type = 'telegram_text'
     AND (intake.parser_output_id = existing.id
          OR intake.public_id = existing.source_public_id)
    WHERE existing.id = NEW.id OR existing.public_id = NEW.public_id
)
BEGIN
    SELECT RAISE(ABORT, 'Telegram text parser identity collision');
END;

CREATE TRIGGER trg_finance_telegram_text_parser_no_update_collision
BEFORE UPDATE ON parser_outputs
WHEN EXISTS (
    SELECT 1 FROM parser_outputs AS existing
    JOIN raw_intake_records AS intake
      ON intake.source_type = 'telegram_text'
     AND (intake.parser_output_id = existing.id
          OR intake.public_id = existing.source_public_id)
    WHERE existing.id <> OLD.id
      AND (existing.id = NEW.id OR existing.public_id = NEW.public_id)
)
BEGIN
    SELECT RAISE(ABORT, 'Telegram text parser identity collision');
END;

CREATE TRIGGER trg_finance_interaction_no_control_parser
BEFORE INSERT ON parser_outputs
WHEN EXISTS (
    SELECT 1 FROM raw_intake_records AS intake
    LEFT JOIN finance_capture_jobs AS job ON job.raw_intake_record_id = intake.id
    LEFT JOIN finance_capture_interaction_routes AS route
      ON route.job_public_id = job.public_id
    WHERE intake.public_id = NEW.source_public_id
      AND intake.source_type = 'telegram_text'
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

-- Parser identity is fixed once it refers to a Telegram text source.  This
-- closes the insert-with-NULL then UPDATE source binding escape.
CREATE TRIGGER trg_finance_telegram_text_parser_identity
BEFORE UPDATE ON parser_outputs
WHEN (NEW.id IS NOT OLD.id OR NEW.public_id IS NOT OLD.public_id
      OR NEW.source_type IS NOT OLD.source_type
      OR NEW.source_public_id IS NOT OLD.source_public_id
      OR NEW.parent_parser_output_id IS NOT OLD.parent_parser_output_id
      OR NEW.attachment_id IS NOT OLD.attachment_id)
 AND (EXISTS (SELECT 1 FROM raw_intake_records AS intake
              WHERE intake.public_id = OLD.source_public_id
                AND intake.source_type = 'telegram_text')
      OR EXISTS (SELECT 1 FROM raw_intake_records AS intake
                 WHERE intake.public_id = NEW.source_public_id
                   AND intake.source_type = 'telegram_text'))
BEGIN
    SELECT RAISE(ABORT, 'Telegram text parser source identity is immutable');
END;

-- Binding a raw source to a parser is another authority transition.  The
-- historical admission permits only a direct child of its current pointer.
CREATE TRIGGER trg_finance_telegram_text_pointer_source
BEFORE UPDATE OF parser_output_id ON raw_intake_records
WHEN OLD.source_type = 'telegram_text'
 AND NEW.parser_output_id IS NOT OLD.parser_output_id
 AND NEW.parser_output_id IS NOT NULL
 AND NOT EXISTS (
     SELECT 1 FROM parser_outputs AS proposal
     WHERE proposal.id = NEW.parser_output_id
       AND proposal.source_type = 'telegram_text'
       AND (proposal.source_public_id = OLD.public_id
            OR EXISTS (
                SELECT 1 FROM ai_fallback_proposal_links AS link
                JOIN ai_fallback_results AS result ON result.id = link.result_id
                JOIN ai_fallback_attempts AS attempt ON attempt.id = result.attempt_id
                WHERE link.parser_output_id = proposal.id
                  AND attempt.raw_intake_record_id = OLD.id
                  AND attempt.parent_parser_output_id = OLD.parser_output_id
                  AND proposal.parent_parser_output_id = OLD.parser_output_id
            ))
 )
BEGIN
    SELECT RAISE(ABORT, 'Telegram text parser pointer source mismatch');
END;

CREATE TRIGGER trg_finance_telegram_text_parser_pointer
BEFORE UPDATE OF parser_output_id ON raw_intake_records
WHEN OLD.source_type = 'telegram_text'
 AND NEW.parser_output_id IS NOT OLD.parser_output_id
 AND NOT EXISTS (
     SELECT 1 FROM finance_capture_jobs AS job
     JOIN finance_capture_interaction_routes AS route
       ON route.job_public_id = job.public_id
     WHERE job.raw_intake_record_id = OLD.id
       AND job.capture_kind = 'text' AND route.route_kind = 'initial_intake'
 )
 AND NOT EXISTS (
     SELECT 1 FROM finance_legacy_text_lineage_admissions AS legacy
     JOIN parser_outputs AS child ON child.id = NEW.parser_output_id
     WHERE legacy.raw_intake_record_id = OLD.id
       AND legacy.source_public_id = OLD.public_id
       AND OLD.parser_output_id IS NOT NULL
       AND child.parent_parser_output_id = OLD.parser_output_id
       AND child.source_type = 'telegram_text'
       AND (child.source_public_id = OLD.public_id
            OR EXISTS (
                SELECT 1 FROM ai_fallback_proposal_links AS link
                JOIN ai_fallback_results AS result ON result.id = link.result_id
                JOIN ai_fallback_attempts AS attempt ON attempt.id = result.attempt_id
                WHERE link.parser_output_id = child.id
                  AND attempt.raw_intake_record_id = OLD.id
                  AND attempt.parent_parser_output_id = OLD.parser_output_id
            ))
 )
BEGIN
    SELECT RAISE(ABORT, 'Telegram text requires initial intake route before parser binding');
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
