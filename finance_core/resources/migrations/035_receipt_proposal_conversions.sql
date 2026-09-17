PRAGMA foreign_keys = ON;

-- Append-only receipt proposal-to-facts conversion registry (B4.1).
--
-- One row binds one authenticated human conversion command to exactly one
-- confirmed receipt total proposal, exactly one confirmation record, and
-- exactly one receipts fact row.  The registry is the durable idempotency
-- store for the guarded conversion service: replay of the same command
-- returns the recorded result instead of writing twice.  Conversion creates
-- receipt facts only: it never creates transactions, calculations, or
-- settlement obligations.
--
-- Deterministic replay/conflict contract:
--   * command_public_id is caller-owned and the primary key, so the same
--     command replays against the persisted row instead of writing twice;
--   * parser_output_id is unique, so one proposal converts at most once;
--   * supersession_root_parser_output_id is unique, giving the
--     one-fact-set-per-supersession-chain invariant a schema backstop even
--     if a service-level chain walk is bypassed (for an unsuperseded
--     proposal the chain root is the proposal itself);
--   * receipt_id is unique, so each receipt fact set traces back to exactly
--     one conversion command;
--   * confirmation_public_id is unique, so each human confirmation
--     authorizes at most one conversion.

CREATE TABLE IF NOT EXISTS receipt_proposal_conversions (
    command_public_id TEXT PRIMARY KEY NOT NULL CHECK (
        length(command_public_id) BETWEEN 6 AND 200
        AND substr(command_public_id, 1, 5) = 'rpfc_'
        AND command_public_id NOT GLOB '*[^A-Za-z0-9_-]*'
    ),
    parser_output_id INTEGER NOT NULL UNIQUE,
    supersession_root_parser_output_id INTEGER NOT NULL UNIQUE,
    receipt_id INTEGER NOT NULL UNIQUE,
    confirmation_public_id TEXT NOT NULL UNIQUE,
    proposal_content_hash TEXT NOT NULL CHECK (
        length(proposal_content_hash) = 64
        AND lower(proposal_content_hash) = proposal_content_hash
        AND proposal_content_hash NOT GLOB '*[^0-9a-f]*'
    ),
    command_material_hash TEXT NOT NULL CHECK (
        length(command_material_hash) = 64
        AND lower(command_material_hash) = command_material_hash
        AND command_material_hash NOT GLOB '*[^0-9a-f]*'
    ),
    conversion_result_hash TEXT NOT NULL CHECK (
        length(conversion_result_hash) = 64
        AND lower(conversion_result_hash) = conversion_result_hash
        AND conversion_result_hash NOT GLOB '*[^0-9a-f]*'
    ),
    actor_type TEXT NOT NULL CHECK (actor_type = 'human'),
    authenticated_actor_id TEXT NOT NULL CHECK (length(trim(authenticated_actor_id)) > 0),
    conversion_channel TEXT NOT NULL CHECK (length(trim(conversion_channel)) > 0),
    -- Non-authoritative metadata: stored for audit context, excluded from
    -- command material and replay/conflict comparison (mirrors migration
    -- 034's revision reason column).
    reason TEXT,
    schema_version TEXT NOT NULL DEFAULT 'v1' CHECK (schema_version = 'v1'),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (parser_output_id) REFERENCES parser_outputs(id),
    FOREIGN KEY (supersession_root_parser_output_id) REFERENCES parser_outputs(id),
    FOREIGN KEY (receipt_id) REFERENCES receipts(id),
    FOREIGN KEY (confirmation_public_id)
        REFERENCES parser_proposal_authorizations(confirmation_public_id)
);

-- All registry lookups used by the conversion service (command replay by
-- command_public_id, proposal/chain guards by parser_output_id and
-- supersession_root_parser_output_id, receipt lineage by receipt_id,
-- confirmation binding by confirmation_public_id) are served by the primary
-- key and the UNIQUE constraints' implicit indexes; no additional indexes
-- are required by actual queries.

-- Schema backstop for the one-receipt-fact-row-per-proposal invariant on
-- the facts table itself (design Section 4.2 evaluation: adopted).
-- Migration 002 left receipts.parser_output_id non-unique; every existing
-- writer (seed test cases, finalization fixtures) leaves it NULL, so this
-- partial unique index is purely additive.
CREATE UNIQUE INDEX IF NOT EXISTS idx_receipts_parser_output_id_unique
    ON receipts(parser_output_id)
    WHERE parser_output_id IS NOT NULL;

-- -----------------------------------------------------------------------
-- Append-only enforcement
-- -----------------------------------------------------------------------

CREATE TRIGGER IF NOT EXISTS trg_receipt_proposal_conversions_no_update
    BEFORE UPDATE ON receipt_proposal_conversions
BEGIN
    SELECT RAISE(ABORT, 'receipt_proposal_conversions rows are append-only');
END;

CREATE TRIGGER IF NOT EXISTS trg_receipt_proposal_conversions_no_delete
    BEFORE DELETE ON receipt_proposal_conversions
BEGIN
    SELECT RAISE(ABORT, 'receipt_proposal_conversions rows are append-only');
END;

-- INSERT OR REPLACE resolves UNIQUE/PRIMARY KEY collisions by an implicit
-- DELETE that, with recursive_triggers off (the SQLite default), bypasses
-- the BEFORE DELETE trigger above and silently rewrites history.  This
-- BEFORE INSERT trigger fires before conflict resolution, so any insert
-- colliding with one of the five registry identities aborts regardless of
-- the conflict clause.  Plain non-colliding INSERTs are unaffected.
CREATE TRIGGER IF NOT EXISTS trg_receipt_proposal_conversions_no_insert_collision
    BEFORE INSERT ON receipt_proposal_conversions
    WHEN EXISTS (
        SELECT 1
        FROM receipt_proposal_conversions
        WHERE command_public_id = NEW.command_public_id
           OR parser_output_id = NEW.parser_output_id
           OR supersession_root_parser_output_id
              = NEW.supersession_root_parser_output_id
           OR receipt_id = NEW.receipt_id
           OR confirmation_public_id = NEW.confirmation_public_id
    )
BEGIN
    SELECT RAISE(
        ABORT,
        'UNIQUE conversion identity collision: receipt_proposal_conversions rows are append-only and cannot be replaced'
    );
END;

-- The financial audit chain (migration 025) has the same INSERT OR REPLACE
-- exposure on its five event identities.  Migration 025 is historical and
-- must not be edited, so the additive backstop lives here alongside the
-- registry protection the conversion path depends on.
CREATE TRIGGER IF NOT EXISTS trg_financial_audit_events_no_insert_collision
    BEFORE INSERT ON financial_audit_events
    WHEN EXISTS (
        SELECT 1
        FROM financial_audit_events
        WHERE event_public_id = NEW.event_public_id
           OR event_hash = NEW.event_hash
           OR (
               aggregate_type = NEW.aggregate_type
               AND aggregate_public_id = NEW.aggregate_public_id
               AND (
                   sequence_number = NEW.sequence_number
                   OR previous_event_hash = NEW.previous_event_hash
                   OR (
                       event_type = NEW.event_type
                       AND causation_public_id = NEW.causation_public_id
                   )
               )
           )
    )
BEGIN
    SELECT RAISE(
        ABORT,
        'UNIQUE audit event identity collision: financial_audit_events rows are append-only and cannot be replaced'
    );
END;

-- -----------------------------------------------------------------------
-- Raw input durable binding backstop
-- -----------------------------------------------------------------------

-- Once a raw intake record is bound into a receipt proposal lineage
-- (directly via parser_output_id or referenced by a parser output's
-- source_public_id), the raw input content and its durable identity
-- columns (integer primary key, public_id, source_content_hash,
-- content_fingerprint from migration 022) are frozen.  Lifecycle-only
-- updates (status, normalized_text, timestamps) remain allowed.  Rows
-- without established lineage are untouched, so intake-time backfills
-- stay legal.  A plain BEFORE UPDATE (no OF column list) is required:
-- UPDATE ... SET rowid/_rowid_/oid changes the INTEGER PRIMARY KEY
-- without naming id in the SET list, so an OF-list trigger would never
-- fire; the WHEN clause already limits firing to identity drift.
CREATE TRIGGER IF NOT EXISTS trg_raw_intake_records_freeze_source_identity
    BEFORE UPDATE
    ON raw_intake_records
    FOR EACH ROW
    WHEN (
        NEW.id IS NOT OLD.id
        OR NEW.public_id IS NOT OLD.public_id
        OR NEW.raw_input IS NOT OLD.raw_input
        OR NEW.source_content_hash IS NOT OLD.source_content_hash
        OR NEW.content_fingerprint IS NOT OLD.content_fingerprint
        OR NEW.fingerprint_version IS NOT OLD.fingerprint_version
    )
    AND (
        OLD.parser_output_id IS NOT NULL
        OR EXISTS (
            SELECT 1 FROM parser_outputs
            WHERE source_public_id = OLD.public_id
        )
    )
BEGIN
    SELECT RAISE(
        ABORT,
        'raw intake source identity is frozen once receipt proposal lineage is established'
    );
END;

-- The proposal lineage pointer itself must not be used to escape the
-- freeze: once parser_output_id is established it may only be repointed
-- to a direct child of the current parser output (the guarded
-- supersession/revision workflow).  The first legal bind (NULL -> id)
-- stays open for B1 intake, and detaching (id -> NULL) or retargeting to
-- an unrelated parser output is rejected.
CREATE TRIGGER IF NOT EXISTS trg_raw_intake_records_pointer_lineage_control
    BEFORE UPDATE OF parser_output_id ON raw_intake_records
    FOR EACH ROW
    WHEN OLD.parser_output_id IS NOT NULL
        AND NEW.parser_output_id IS NOT OLD.parser_output_id
        AND NOT EXISTS (
            SELECT 1 FROM parser_outputs
            WHERE id = NEW.parser_output_id
              AND parent_parser_output_id = OLD.parser_output_id
        )
BEGIN
    SELECT RAISE(
        ABORT,
        'raw intake proposal lineage pointer cannot be detached or retargeted once established'
    );
END;

-- INSERT OR REPLACE resolves collisions on any raw-intake UNIQUE identity
-- (rowid primary key, public_id, the partial unique idempotency_key
-- index) with an implicit DELETE that bypasses BEFORE DELETE triggers
-- when recursive_triggers is off (the SQLite default).  Any insert that
-- collides with a lineage-bound row aborts before conflict resolution;
-- inserts touching only unbound rows keep their intake semantics.
CREATE TRIGGER IF NOT EXISTS trg_raw_intake_records_no_insert_collision
    BEFORE INSERT ON raw_intake_records
    WHEN EXISTS (
        SELECT 1 FROM raw_intake_records AS existing
        WHERE (
            existing.id = NEW.id
            OR existing.public_id = NEW.public_id
            OR (
                NEW.idempotency_key IS NOT NULL
                AND existing.idempotency_key = NEW.idempotency_key
            )
        )
        AND (
            existing.parser_output_id IS NOT NULL
            OR EXISTS (
                SELECT 1 FROM parser_outputs
                WHERE source_public_id = existing.public_id
            )
        )
    )
BEGIN
    SELECT RAISE(
        ABORT,
        'UNIQUE raw intake identity collision (id, public_id, idempotency_key): lineage-bound raw_intake_records rows cannot be replaced'
    );
END;

-- Direct DELETE protection for lineage-bound rows.  This must not rely on
-- foreign-key enforcement because PRAGMA foreign_keys is per-connection.
CREATE TRIGGER IF NOT EXISTS trg_raw_intake_records_no_delete_lineage_bound
    BEFORE DELETE ON raw_intake_records
    WHEN OLD.parser_output_id IS NOT NULL
        OR EXISTS (
            SELECT 1 FROM parser_outputs
            WHERE source_public_id = OLD.public_id
        )
BEGIN
    SELECT RAISE(
        ABORT,
        'lineage-bound raw_intake_records rows cannot be deleted'
    );
END;

-- UPDATE OR REPLACE resolves a UNIQUE collision exactly like INSERT OR
-- REPLACE: an implicit DELETE of the other (colliding) row that bypasses
-- BEFORE DELETE triggers when recursive_triggers is off.  The freeze
-- triggers above only inspect the row being updated, so an update driven
-- from an unbound row would still destroy a lineage-bound row.  This
-- BEFORE UPDATE guard fires before conflict resolution and aborts when
-- any NEW identity collides with a *different* lineage-bound row,
-- independent of foreign-key enforcement.
CREATE TRIGGER IF NOT EXISTS trg_raw_intake_records_no_update_collision
    BEFORE UPDATE ON raw_intake_records
    FOR EACH ROW
    WHEN EXISTS (
        SELECT 1 FROM raw_intake_records AS existing
        WHERE existing.id <> OLD.id
        AND (
            existing.id = NEW.id
            OR existing.public_id = NEW.public_id
            OR (
                NEW.idempotency_key IS NOT NULL
                AND existing.idempotency_key = NEW.idempotency_key
            )
        )
        AND (
            existing.parser_output_id IS NOT NULL
            OR EXISTS (
                SELECT 1 FROM parser_outputs
                WHERE source_public_id = existing.public_id
            )
        )
    )
BEGIN
    SELECT RAISE(
        ABORT,
        'UNIQUE raw intake identity collision (id, public_id, idempotency_key): lineage-bound raw_intake_records rows cannot be replaced'
    );
END;

-- -----------------------------------------------------------------------
-- Exact canonical monetary representation (conversion-authoritative)
-- -----------------------------------------------------------------------

-- receipts.net_paid_amount (migration 002) has NUMERIC affinity, which is
-- lossy for decimal text: values that look numeric are coerced to INTEGER
-- or IEEE-754 REAL storage.  This additive TEXT column stores the exact
-- canonical minor-unit string written by the conversion service and is the
-- authoritative monetary representation for conversion-created receipt
-- facts (B4.2 must read this column when it is non-NULL and re-run Money
-- Contract validation before trusting it).  The legacy NUMERIC column is
-- retained for compatibility with existing readers and must stay
-- Decimal-equal to this text for conversion rows.  NULL is reserved for
-- legacy rows created outside the conversion path.
--
-- The CHECK enforces the currency-aware canonical lexical form produced
-- by canonical_money_str(): exact minor-unit scale for the row currency,
-- no redundant leading zeros, no sign, no exponent, no whitespace.
-- Currencies outside the explicit registry (mirroring src/money.py) fail
-- closed rather than accepting an unvalidated shape.
ALTER TABLE receipts ADD COLUMN net_paid_amount_canonical_text TEXT CHECK (
    net_paid_amount_canonical_text IS NULL
    OR CASE
        WHEN currency IN ('SGD', 'USD', 'EUR', 'GBP', 'AUD', 'CNY', 'HKD') THEN (
            -- Two minor units: digits '.' digits-2, single dot, no
            -- redundant leading zero ('0.xx' is the only 0-led form).
            net_paid_amount_canonical_text NOT GLOB '*[^0-9.]*'
            AND net_paid_amount_canonical_text GLOB '*.[0-9][0-9]'
            AND instr(net_paid_amount_canonical_text, '.')
                = length(net_paid_amount_canonical_text) - 2
            AND (
                net_paid_amount_canonical_text GLOB '[1-9]*'
                OR (
                    substr(net_paid_amount_canonical_text, 1, 1) = '0'
                    AND length(net_paid_amount_canonical_text) = 4
                )
            )
        )
        WHEN currency = 'JPY' THEN (
            -- Zero minor units: integer digits only, no leading zeros.
            net_paid_amount_canonical_text NOT GLOB '*[^0-9]*'
            AND (
                net_paid_amount_canonical_text = '0'
                OR net_paid_amount_canonical_text GLOB '[1-9]*'
            )
        )
        ELSE 0
    END
);

-- -----------------------------------------------------------------------
-- Conversion-bound receipt facts immutability
-- -----------------------------------------------------------------------

-- Once a receipts row is bound by a receipt_proposal_conversions registry
-- row, its authoritative fact columns are immutable at the schema level:
-- replay verification and the audit chain both bind to these values, so
-- post-commit drift must fail closed.  Any future correction belongs to a
-- separate guarded, audited correction boundary - not to bare SQL.
-- Lifecycle columns (status, notes, transaction_id for future guarded
-- finalization, timestamps) stay mutable, and receipts without a registry
-- binding keep their legacy behaviour entirely.  A plain BEFORE UPDATE
-- (no OF column list) is required: a rowid-alias UPDATE changes id
-- without naming it in the SET list, which would detach the row from
-- every receipt_id-keyed conversion guard; the WHEN clause already
-- limits firing to fact/identity drift.
CREATE TRIGGER IF NOT EXISTS trg_receipts_conversion_bound_freeze
    BEFORE UPDATE
    ON receipts
    FOR EACH ROW
    WHEN (
        NEW.id IS NOT OLD.id
        OR NEW.public_id IS NOT OLD.public_id
        OR NEW.merchant IS NOT OLD.merchant
        OR NEW.receipt_datetime IS NOT OLD.receipt_datetime
        OR NEW.net_paid_amount IS NOT OLD.net_paid_amount
        OR NEW.net_paid_amount_canonical_text
            IS NOT OLD.net_paid_amount_canonical_text
        OR NEW.currency IS NOT OLD.currency
        OR NEW.payer_participant_id IS NOT OLD.payer_participant_id
        OR NEW.source_channel IS NOT OLD.source_channel
        OR NEW.raw_input IS NOT OLD.raw_input
        OR NEW.attachment_id IS NOT OLD.attachment_id
        OR NEW.attachment_path IS NOT OLD.attachment_path
        OR NEW.ocr_confidence IS NOT OLD.ocr_confidence
        OR NEW.parser_output_id IS NOT OLD.parser_output_id
    )
    AND EXISTS (
        SELECT 1 FROM receipt_proposal_conversions
        WHERE receipt_id = OLD.id
    )
BEGIN
    SELECT RAISE(
        ABORT,
        'conversion-bound receipt facts are immutable outside a guarded correction boundary'
    );
END;

CREATE TRIGGER IF NOT EXISTS trg_receipts_conversion_bound_no_delete
    BEFORE DELETE ON receipts
    WHEN EXISTS (
        SELECT 1 FROM receipt_proposal_conversions
        WHERE receipt_id = OLD.id
    )
BEGIN
    SELECT RAISE(
        ABORT,
        'conversion-bound receipt facts are immutable outside a guarded correction boundary'
    );
END;

-- INSERT OR REPLACE protection: an insert colliding with a
-- conversion-bound receipt on any UNIQUE identity (rowid primary key,
-- public_id, the partial unique parser_output_id index above) aborts
-- before conflict resolution, so REPLACE's implicit DELETE cannot rewrite
-- registry-bound facts even with recursive_triggers off.
CREATE TRIGGER IF NOT EXISTS trg_receipts_conversion_bound_no_insert_collision
    BEFORE INSERT ON receipts
    WHEN EXISTS (
        SELECT 1
        FROM receipts AS existing
        JOIN receipt_proposal_conversions AS binding
            ON binding.receipt_id = existing.id
        WHERE existing.id = NEW.id
           OR existing.public_id = NEW.public_id
           OR (
               NEW.parser_output_id IS NOT NULL
               AND existing.parser_output_id = NEW.parser_output_id
           )
    )
BEGIN
    SELECT RAISE(
        ABORT,
        'conversion-bound receipt facts are immutable outside a guarded correction boundary'
    );
END;

-- UPDATE OR REPLACE protection: like the raw-intake guard above, an
-- update of an unbound receipt whose NEW identity collides with a
-- registry-bound receipt would REPLACE-delete the bound row without
-- firing its DELETE trigger.  Abort before conflict resolution whenever
-- any NEW identity (rowid primary key, public_id, the partial unique
-- parser_output_id index) collides with a *different* bound row.
CREATE TRIGGER IF NOT EXISTS trg_receipts_conversion_bound_no_update_collision
    BEFORE UPDATE ON receipts
    FOR EACH ROW
    WHEN EXISTS (
        SELECT 1
        FROM receipts AS existing
        JOIN receipt_proposal_conversions AS binding
            ON binding.receipt_id = existing.id
        WHERE existing.id <> OLD.id
        AND (
            existing.id = NEW.id
            OR existing.public_id = NEW.public_id
            OR (
                NEW.parser_output_id IS NOT NULL
                AND existing.parser_output_id = NEW.parser_output_id
            )
        )
    )
BEGIN
    SELECT RAISE(
        ABORT,
        'conversion-bound receipt facts are immutable outside a guarded correction boundary'
    );
END;

-- -----------------------------------------------------------------------
-- Conversion-bound receipt membership immutability (round 4)
-- -----------------------------------------------------------------------

-- Membership rows written by the conversion are authoritative Facts: once
-- their receipt is registry-bound, the deterministic membership set
-- (derived public IDs, participant linkage, role semantics, inclusion,
-- and row count) is immutable at the schema level.  No INSERT, UPDATE,
-- or DELETE may touch it, and REPLACE conflict resolution (an implicit
-- DELETE that bypasses DELETE triggers with recursive_triggers off) is
-- aborted before it can rewrite a bound row on any UNIQUE identity:
-- rowid primary key, public_id, or (receipt_id, participant_id).  The
-- conversion service itself writes membership before the registry row,
-- so these backstops never fire on its own write path.  Receipts without
-- a registry binding keep their legacy mutable membership behaviour.
CREATE TRIGGER IF NOT EXISTS trg_receipt_participants_conversion_bound_freeze
    BEFORE UPDATE ON receipt_participants
    FOR EACH ROW
    WHEN EXISTS (
        SELECT 1 FROM receipt_proposal_conversions
        WHERE receipt_id = OLD.receipt_id
           OR receipt_id = NEW.receipt_id
    )
BEGIN
    SELECT RAISE(
        ABORT,
        'conversion-bound receipt membership is immutable outside a guarded correction boundary'
    );
END;

CREATE TRIGGER IF NOT EXISTS trg_receipt_participants_conversion_bound_no_delete
    BEFORE DELETE ON receipt_participants
    WHEN EXISTS (
        SELECT 1 FROM receipt_proposal_conversions
        WHERE receipt_id = OLD.receipt_id
    )
BEGIN
    SELECT RAISE(
        ABORT,
        'conversion-bound receipt membership is immutable outside a guarded correction boundary'
    );
END;

CREATE TRIGGER IF NOT EXISTS trg_receipt_participants_conversion_bound_no_insert
    BEFORE INSERT ON receipt_participants
    WHEN EXISTS (
        SELECT 1 FROM receipt_proposal_conversions
        WHERE receipt_id = NEW.receipt_id
    )
BEGIN
    SELECT RAISE(
        ABORT,
        'conversion-bound receipt membership is immutable outside a guarded correction boundary'
    );
END;

-- INSERT OR REPLACE protection: an insert colliding with a bound
-- membership row on any UNIQUE identity aborts before conflict
-- resolution, so REPLACE's implicit DELETE cannot rewrite the bound set
-- even with recursive_triggers off.
CREATE TRIGGER IF NOT EXISTS trg_receipt_participants_no_insert_collision
    BEFORE INSERT ON receipt_participants
    WHEN EXISTS (
        SELECT 1
        FROM receipt_participants AS existing
        JOIN receipt_proposal_conversions AS binding
            ON binding.receipt_id = existing.receipt_id
        WHERE existing.id = NEW.id
           OR existing.public_id = NEW.public_id
           OR (
               existing.receipt_id = NEW.receipt_id
               AND existing.participant_id = NEW.participant_id
           )
    )
BEGIN
    SELECT RAISE(
        ABORT,
        'UNIQUE receipt membership identity collision (id, public_id, receipt_id+participant_id): conversion-bound membership rows cannot be replaced'
    );
END;

-- UPDATE OR REPLACE protection: an update of an unbound membership row
-- whose NEW identity collides with a *different* bound row would
-- REPLACE-delete the bound row without firing its DELETE trigger.
CREATE TRIGGER IF NOT EXISTS trg_receipt_participants_no_update_collision
    BEFORE UPDATE ON receipt_participants
    FOR EACH ROW
    WHEN EXISTS (
        SELECT 1
        FROM receipt_participants AS existing
        JOIN receipt_proposal_conversions AS binding
            ON binding.receipt_id = existing.receipt_id
        WHERE existing.id <> OLD.id
        AND (
            existing.id = NEW.id
            OR existing.public_id = NEW.public_id
            OR (
                existing.receipt_id = NEW.receipt_id
                AND existing.participant_id = NEW.participant_id
            )
        )
    )
BEGIN
    SELECT RAISE(
        ABORT,
        'UNIQUE receipt membership identity collision (id, public_id, receipt_id+participant_id): conversion-bound membership rows cannot be replaced'
    );
END;

-- Registry rows may only bind receipts that carry the authoritative
-- canonical monetary text: a conversion-created fact row without it would
-- silently downgrade B4.2 readers to the lossy NUMERIC mirror.  A
-- nonexistent receipt_id is left to foreign-key enforcement.
CREATE TRIGGER IF NOT EXISTS trg_receipt_proposal_conversions_require_canonical_amount
    BEFORE INSERT ON receipt_proposal_conversions
    WHEN EXISTS (
        SELECT 1 FROM receipts
        WHERE id = NEW.receipt_id
          AND net_paid_amount_canonical_text IS NULL
    )
BEGIN
    SELECT RAISE(
        ABORT,
        'conversion registry rows require a receipt with canonical monetary text'
    );
END;
