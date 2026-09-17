-- Migration 036: receipt item & allocation facts schema (IAF.1)
--
-- Implements the approved persistence model of
-- docs/design/receipt_item_allocation_facts_boundary_v1.md (Sections 9-11),
-- schema and constraint backstops only.  No service, CLI, readiness,
-- projection, or supersession runtime is created here (IAF.2+ slices).
--
-- Additive only:
--   * new fact-set registry table receipt_item_allocation_fact_sets;
--   * nullable columns on receipt_items and receipt_adjustments;
--   * new allocation fact table receipt_item_allocation_facts;
--   * partial unique indexes and append-only/collision/freeze triggers.
-- Migrations 001-035 are untouched; legacy rows (NULL fact_set_id) keep
-- their pre-existing behaviour and are never adopted into a fact set.
--
-- The frozen Section 11.1 supersession write model is binding:
--   * supersedes_fact_set_public_id is an ordinary immediate FK (the
--     predecessor row exists before the successor insert);
--   * superseded_by_fact_set_public_id is DEFERRABLE INITIALLY DEFERRED
--     (the transition executes before the successor row exists; the FK is
--     enforced at COMMIT with PRAGMA foreign_keys = ON);
--   * one-active partial unique index on receipt_id WHERE
--     superseded_by_fact_set_public_id IS NULL;
--   * append-only triggers permit exactly one UPDATE shape: the single
--     NULL -> rfs_-shaped successor transition of
--     superseded_by_fact_set_public_id with every other column unchanged.

-- -----------------------------------------------------------------------
-- Fact-set registry
-- -----------------------------------------------------------------------

-- One row per fact-set version.  command_public_id is the caller-owned
-- idempotency identity (riaf_ create / riafc_ correction, Section 12.4);
-- fact_set_public_id is the deterministic rfs_ derivation known before
-- any write, which is what makes the transition-first ordering possible.
CREATE TABLE IF NOT EXISTS receipt_item_allocation_fact_sets (
    command_public_id TEXT PRIMARY KEY NOT NULL CHECK (
        command_public_id NOT GLOB '*[^A-Za-z0-9_-]*'
        AND (
            (
                substr(command_public_id, 1, 5) = 'riaf_'
                AND length(command_public_id) BETWEEN 6 AND 200
            )
            OR (
                substr(command_public_id, 1, 6) = 'riafc_'
                AND length(command_public_id) BETWEEN 7 AND 200
            )
        )
    ),
    fact_set_public_id TEXT NOT NULL UNIQUE CHECK (
        substr(fact_set_public_id, 1, 4) = 'rfs_'
        AND length(fact_set_public_id) BETWEEN 5 AND 200
        AND fact_set_public_id NOT GLOB '*[^A-Za-z0-9_-]*'
    ),
    receipt_id INTEGER NOT NULL,
    version INTEGER NOT NULL CHECK (version >= 1),
    conversion_command_public_id TEXT NOT NULL CHECK (
        substr(conversion_command_public_id, 1, 5) = 'rpfc_'
        AND length(conversion_command_public_id) BETWEEN 6 AND 200
        AND conversion_command_public_id NOT GLOB '*[^A-Za-z0-9_-]*'
    ),
    expected_conversion_result_hash TEXT NOT NULL CHECK (
        length(expected_conversion_result_hash) = 64
        AND lower(expected_conversion_result_hash) = expected_conversion_result_hash
        AND expected_conversion_result_hash NOT GLOB '*[^0-9a-f]*'
    ),
    -- Immediate FK: the predecessor always exists before the successor
    -- insert (Section 11.1), so no deferral is needed here.
    supersedes_fact_set_public_id TEXT
        REFERENCES receipt_item_allocation_fact_sets(fact_set_public_id)
        CHECK (
            supersedes_fact_set_public_id IS NULL
            OR (
                substr(supersedes_fact_set_public_id, 1, 4) = 'rfs_'
                AND length(supersedes_fact_set_public_id) BETWEEN 5 AND 200
                AND supersedes_fact_set_public_id NOT GLOB '*[^A-Za-z0-9_-]*'
            )
        ),
    -- Deferred FK (mandatory, Section 11.1 proofs P3/P4): the predecessor
    -- transition points at the successor public ID before the successor
    -- row exists; the constraint is enforced at COMMIT.  A missing
    -- successor makes COMMIT itself fail closed.
    superseded_by_fact_set_public_id TEXT
        REFERENCES receipt_item_allocation_fact_sets(fact_set_public_id)
        DEFERRABLE INITIALLY DEFERRED
        CHECK (
            superseded_by_fact_set_public_id IS NULL
            OR (
                substr(superseded_by_fact_set_public_id, 1, 4) = 'rfs_'
                AND length(superseded_by_fact_set_public_id) BETWEEN 5 AND 200
                AND superseded_by_fact_set_public_id NOT GLOB '*[^A-Za-z0-9_-]*'
            )
        ),
    command_material_hash TEXT NOT NULL CHECK (
        length(command_material_hash) = 64
        AND lower(command_material_hash) = command_material_hash
        AND command_material_hash NOT GLOB '*[^0-9a-f]*'
    ),
    fact_set_input_hash TEXT NOT NULL CHECK (
        length(fact_set_input_hash) = 64
        AND lower(fact_set_input_hash) = fact_set_input_hash
        AND fact_set_input_hash NOT GLOB '*[^0-9a-f]*'
    ),
    fact_set_result_hash TEXT NOT NULL CHECK (
        length(fact_set_result_hash) = 64
        AND lower(fact_set_result_hash) = fact_set_result_hash
        AND fact_set_result_hash NOT GLOB '*[^0-9a-f]*'
    ),
    canonical_fact_set_payload TEXT NOT NULL CHECK (
        json_valid(canonical_fact_set_payload) = 1
    ),
    actor_type TEXT NOT NULL CHECK (actor_type = 'human'),
    authenticated_actor_id TEXT NOT NULL CHECK (
        length(trim(authenticated_actor_id)) > 0
    ),
    channel TEXT NOT NULL CHECK (length(trim(channel)) > 0),
    -- Non-authoritative metadata (excluded from command material, mirrors
    -- migrations 034/035).  Bounded per the approved Section 5.1 contract;
    -- SQLite length() counts code points, matching Python len().
    reason TEXT CHECK (
        reason IS NULL
        OR (length(trim(reason)) > 0 AND length(reason) <= 500)
    ),
    -- Deferred FK: the Section 13 fixed insert order is registry ->
    -- items -> allocations -> adjustments -> audit event, so the audit
    -- row does not exist yet when the registry row is inserted.  The
    -- binding stays fail-closed at COMMIT (a registry row can never be
    -- committed without its audit event).
    audit_event_public_id TEXT NOT NULL UNIQUE
        REFERENCES financial_audit_events(event_public_id)
        DEFERRABLE INITIALLY DEFERRED
        CHECK (length(trim(audit_event_public_id)) > 0),
    schema_version TEXT NOT NULL DEFAULT 'v1' CHECK (schema_version = 'v1'),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (receipt_id) REFERENCES receipts(id),
    FOREIGN KEY (conversion_command_public_id)
        REFERENCES receipt_proposal_conversions(command_public_id),
    UNIQUE (receipt_id, version),
    -- Version lineage coupling: version 1 has no predecessor; every
    -- correction version records exactly one.
    CHECK (
        (version = 1 AND supersedes_fact_set_public_id IS NULL)
        OR (version > 1 AND supersedes_fact_set_public_id IS NOT NULL)
    ),
    -- No self-reference in either direction of the supersession chain.
    CHECK (
        supersedes_fact_set_public_id IS NULL
        OR supersedes_fact_set_public_id <> fact_set_public_id
    ),
    CHECK (
        superseded_by_fact_set_public_id IS NULL
        OR superseded_by_fact_set_public_id <> fact_set_public_id
    )
);

-- One-active-set backstop (Section 10.1/11.1): two active fact sets for
-- one receipt are impossible at schema level.  This partial index is also
-- what makes the successor-insert-first ordering fail (proof P2).
CREATE UNIQUE INDEX IF NOT EXISTS idx_receipt_item_allocation_fact_sets_one_active
    ON receipt_item_allocation_fact_sets(receipt_id)
    WHERE superseded_by_fact_set_public_id IS NULL;

-- -----------------------------------------------------------------------
-- Fact-set registry append-only enforcement
-- -----------------------------------------------------------------------

CREATE TRIGGER IF NOT EXISTS trg_receipt_item_allocation_fact_sets_no_delete
    BEFORE DELETE ON receipt_item_allocation_fact_sets
BEGIN
    SELECT RAISE(
        ABORT,
        'receipt_item_allocation_fact_sets rows are append-only'
    );
END;

-- The single permitted UPDATE is the Section 11.1 supersession transition:
-- superseded_by_fact_set_public_id NULL -> rfs_-shaped successor ID, with
-- every other column (including the rowid alias) byte-identical.  A plain
-- BEFORE UPDATE (no OF column list) is required: UPDATE ... SET rowid
-- changes the implicit rowid without naming any declared column, so an
-- OF-list trigger would never fire.
CREATE TRIGGER IF NOT EXISTS trg_receipt_item_allocation_fact_sets_single_transition
    BEFORE UPDATE ON receipt_item_allocation_fact_sets
    FOR EACH ROW
    WHEN NOT (
        OLD.superseded_by_fact_set_public_id IS NULL
        AND NEW.superseded_by_fact_set_public_id IS NOT NULL
        AND substr(NEW.superseded_by_fact_set_public_id, 1, 4) = 'rfs_'
        AND length(NEW.superseded_by_fact_set_public_id) BETWEEN 5 AND 200
        AND NEW.superseded_by_fact_set_public_id NOT GLOB '*[^A-Za-z0-9_-]*'
        AND NEW.superseded_by_fact_set_public_id <> NEW.fact_set_public_id
        AND NEW.rowid IS OLD.rowid
        AND NEW.command_public_id IS OLD.command_public_id
        AND NEW.fact_set_public_id IS OLD.fact_set_public_id
        AND NEW.receipt_id IS OLD.receipt_id
        AND NEW.version IS OLD.version
        AND NEW.conversion_command_public_id IS OLD.conversion_command_public_id
        AND NEW.expected_conversion_result_hash
            IS OLD.expected_conversion_result_hash
        AND NEW.supersedes_fact_set_public_id IS OLD.supersedes_fact_set_public_id
        AND NEW.command_material_hash IS OLD.command_material_hash
        AND NEW.fact_set_input_hash IS OLD.fact_set_input_hash
        AND NEW.fact_set_result_hash IS OLD.fact_set_result_hash
        AND NEW.canonical_fact_set_payload IS OLD.canonical_fact_set_payload
        AND NEW.actor_type IS OLD.actor_type
        AND NEW.authenticated_actor_id IS OLD.authenticated_actor_id
        AND NEW.channel IS OLD.channel
        AND NEW.reason IS OLD.reason
        AND NEW.audit_event_public_id IS OLD.audit_event_public_id
        AND NEW.schema_version IS OLD.schema_version
        AND NEW.created_at IS OLD.created_at
    )
BEGIN
    SELECT RAISE(
        ABORT,
        'receipt_item_allocation_fact_sets rows are append-only except the single NULL to successor supersession transition'
    );
END;

-- INSERT OR REPLACE resolves UNIQUE/PRIMARY KEY collisions by an implicit
-- DELETE that, with recursive_triggers off (the SQLite default), bypasses
-- the BEFORE DELETE trigger above and silently rewrites history.  This
-- BEFORE INSERT trigger fires before conflict resolution, so any insert
-- colliding with a protected registry identity (command ID, fact-set ID,
-- receipt+version, audit event binding, or the one-active receipt slot)
-- aborts regardless of the conflict clause.  It is also independent of
-- PRAGMA foreign_keys, which is per-connection.
CREATE TRIGGER IF NOT EXISTS trg_receipt_item_allocation_fact_sets_no_insert_collision
    BEFORE INSERT ON receipt_item_allocation_fact_sets
    WHEN EXISTS (
        SELECT 1
        FROM receipt_item_allocation_fact_sets AS existing
        WHERE existing.command_public_id = NEW.command_public_id
           OR existing.fact_set_public_id = NEW.fact_set_public_id
           OR existing.audit_event_public_id = NEW.audit_event_public_id
           OR (
               existing.receipt_id = NEW.receipt_id
               AND existing.version = NEW.version
           )
           OR (
               NEW.superseded_by_fact_set_public_id IS NULL
               AND existing.receipt_id = NEW.receipt_id
               AND existing.superseded_by_fact_set_public_id IS NULL
           )
    )
BEGIN
    SELECT RAISE(
        ABORT,
        'UNIQUE fact set identity collision: receipt_item_allocation_fact_sets rows are append-only and cannot be replaced'
    );
END;

-- Rows are born active: the only way a row becomes superseded is the
-- single audited transition above.  Without this guard an insert could
-- carry a pre-set pointer and dodge both the one-active partial index and
-- the transition trigger.
CREATE TRIGGER IF NOT EXISTS trg_receipt_item_allocation_fact_sets_insert_born_active
    BEFORE INSERT ON receipt_item_allocation_fact_sets
    WHEN NEW.superseded_by_fact_set_public_id IS NOT NULL
BEGIN
    SELECT RAISE(
        ABORT,
        'fact set registry rows must be inserted active with a NULL superseded_by_fact_set_public_id'
    );
END;

-- Conversion-binding backstop: the fact set extends exactly one live B4.1
-- conversion registry row for the same receipt, and the asserted result
-- hash must match the recorded conversion_result_hash byte-exactly.  This
-- also enforces existence independently of per-connection foreign-key
-- enforcement.
CREATE TRIGGER IF NOT EXISTS trg_receipt_item_allocation_fact_sets_conversion_binding
    BEFORE INSERT ON receipt_item_allocation_fact_sets
    WHEN NOT EXISTS (
        SELECT 1
        FROM receipt_proposal_conversions
        WHERE command_public_id = NEW.conversion_command_public_id
          AND receipt_id = NEW.receipt_id
          AND conversion_result_hash = NEW.expected_conversion_result_hash
    )
BEGIN
    SELECT RAISE(
        ABORT,
        'fact set registry rows must bind the live conversion registry row of the same receipt with a matching conversion result hash'
    );
END;

-- Supersession lineage backstop: a correction version's predecessor must
-- be a registry row of the same receipt with exactly the previous version
-- number.  Enforced independently of foreign-key pragma state.
CREATE TRIGGER IF NOT EXISTS trg_receipt_item_allocation_fact_sets_supersedes_lineage
    BEFORE INSERT ON receipt_item_allocation_fact_sets
    WHEN NEW.supersedes_fact_set_public_id IS NOT NULL
        AND NOT EXISTS (
            SELECT 1
            FROM receipt_item_allocation_fact_sets
            WHERE fact_set_public_id = NEW.supersedes_fact_set_public_id
              AND receipt_id = NEW.receipt_id
              AND version = NEW.version - 1
        )
BEGIN
    SELECT RAISE(
        ABORT,
        'supersedes_fact_set_public_id must reference the same-receipt predecessor with the previous version number'
    );
END;

-- -----------------------------------------------------------------------
-- receipt_items additive extension (Section 9.2 / 10.1 option d)
-- -----------------------------------------------------------------------

-- All four columns are nullable and default NULL: every legacy row stays
-- valid and untrusted, and applying this migration rewrites no row.
-- Canonical TEXT is the authoritative monetary representation for
-- fact-set-bound rows; the migration 002 NUMERIC columns become
-- compatibility mirrors only (the migration 035 net_paid precedent).

-- Approved v1 quantity restriction (IA-D12): positive integer canonical
-- text only ('2'), no zero, no sign, no decimals, no leading zeros.
ALTER TABLE receipt_items ADD COLUMN quantity_canonical_text TEXT CHECK (
    quantity_canonical_text IS NULL
    OR (
        quantity_canonical_text NOT GLOB '*[^0-9]*'
        AND quantity_canonical_text GLOB '[1-9]*'
    )
);

-- Currency-aware canonical lexical form from canonical_money_str():
-- exact minor-unit scale for the row currency, no redundant leading
-- zeros, no sign, no exponent, no whitespace.  Zero is excluded because
-- the approved sign policy for item money is STRICTLY_POSITIVE
-- (Section 9.1).  Currencies outside the registry mirrored from
-- src/money.py fail closed.
ALTER TABLE receipt_items ADD COLUMN unit_price_canonical_text TEXT CHECK (
    unit_price_canonical_text IS NULL
    OR CASE
        WHEN currency IN ('SGD', 'USD', 'EUR', 'GBP', 'AUD', 'CNY', 'HKD') THEN (
            unit_price_canonical_text NOT GLOB '*[^0-9.]*'
            AND unit_price_canonical_text GLOB '*.[0-9][0-9]'
            AND instr(unit_price_canonical_text, '.')
                = length(unit_price_canonical_text) - 2
            AND (
                unit_price_canonical_text GLOB '[1-9]*'
                OR (
                    substr(unit_price_canonical_text, 1, 1) = '0'
                    AND length(unit_price_canonical_text) = 4
                )
            )
            AND unit_price_canonical_text <> '0.00'
        )
        WHEN currency = 'JPY' THEN (
            unit_price_canonical_text NOT GLOB '*[^0-9]*'
            AND unit_price_canonical_text GLOB '[1-9]*'
        )
        ELSE 0
    END
);

ALTER TABLE receipt_items ADD COLUMN line_amount_canonical_text TEXT CHECK (
    line_amount_canonical_text IS NULL
    OR CASE
        WHEN currency IN ('SGD', 'USD', 'EUR', 'GBP', 'AUD', 'CNY', 'HKD') THEN (
            line_amount_canonical_text NOT GLOB '*[^0-9.]*'
            AND line_amount_canonical_text GLOB '*.[0-9][0-9]'
            AND instr(line_amount_canonical_text, '.')
                = length(line_amount_canonical_text) - 2
            AND (
                line_amount_canonical_text GLOB '[1-9]*'
                OR (
                    substr(line_amount_canonical_text, 1, 1) = '0'
                    AND length(line_amount_canonical_text) = 4
                )
            )
            AND line_amount_canonical_text <> '0.00'
        )
        WHEN currency = 'JPY' THEN (
            line_amount_canonical_text NOT GLOB '*[^0-9]*'
            AND line_amount_canonical_text GLOB '[1-9]*'
        )
        ELSE 0
    END
);

-- Fact-set binding.  TEXT reference to the registry's deterministic
-- public identity (no surrogate integer identity is invented, Section 10
-- IA-D4).  A bound row must carry the authoritative canonical line amount
-- and an explicit positive line number (line order is hash-semantic,
-- Section 6); legacy rows keep NULL everywhere.
ALTER TABLE receipt_items ADD COLUMN fact_set_id TEXT
    REFERENCES receipt_item_allocation_fact_sets(fact_set_public_id)
    CHECK (
        fact_set_id IS NULL
        OR (
            substr(fact_set_id, 1, 4) = 'rfs_'
            AND length(fact_set_id) BETWEEN 5 AND 200
            AND fact_set_id NOT GLOB '*[^A-Za-z0-9_-]*'
            AND line_number IS NOT NULL
            AND line_number > 0
            AND line_amount_canonical_text IS NOT NULL
        )
    );

-- Fact-set-bound line numbers are unique within their fact set; legacy
-- rows (NULL fact_set_id) are unaffected.
CREATE UNIQUE INDEX IF NOT EXISTS idx_receipt_items_fact_set_line_number
    ON receipt_items(fact_set_id, line_number)
    WHERE fact_set_id IS NOT NULL;

-- Bound item receipt identity must agree with the registry receipt.
-- Also enforces registry existence independently of per-connection
-- foreign-key enforcement.
CREATE TRIGGER IF NOT EXISTS trg_receipt_items_fact_set_receipt_match
    BEFORE INSERT ON receipt_items
    WHEN NEW.fact_set_id IS NOT NULL
        AND NOT EXISTS (
            SELECT 1
            FROM receipt_item_allocation_fact_sets
            WHERE fact_set_public_id = NEW.fact_set_id
              AND receipt_id = NEW.receipt_id
        )
BEGIN
    SELECT RAISE(
        ABORT,
        'fact-set-bound receipt_items rows must reference a fact set registry row for the same receipt'
    );
END;

-- Bound rows are frozen forever, and legacy rows can never be adopted
-- into (or detached from) a fact set by UPDATE.  A plain BEFORE UPDATE is
-- required so rowid-alias updates cannot slip through an OF column list.
-- Legacy-to-legacy updates (both sides NULL) keep their migration 002
-- behaviour.
CREATE TRIGGER IF NOT EXISTS trg_receipt_items_fact_set_bound_freeze
    BEFORE UPDATE ON receipt_items
    FOR EACH ROW
    WHEN OLD.fact_set_id IS NOT NULL
        OR NEW.fact_set_id IS NOT NULL
BEGIN
    SELECT RAISE(
        ABORT,
        'fact-set-bound receipt_items rows are append-only and legacy rows cannot be adopted into a fact set'
    );
END;

CREATE TRIGGER IF NOT EXISTS trg_receipt_items_fact_set_bound_no_delete
    BEFORE DELETE ON receipt_items
    WHEN OLD.fact_set_id IS NOT NULL
BEGIN
    SELECT RAISE(
        ABORT,
        'fact-set-bound receipt_items rows cannot be deleted'
    );
END;

-- REPLACE protection: an insert colliding on any UNIQUE identity (rowid
-- primary key id, public_id, or the fact-set line-number slot) with a
-- bound row - or a bound insert colliding with any row, including a
-- legacy row - aborts before conflict resolution, so REPLACE's implicit
-- DELETE can rewrite neither bound history nor conflicting legacy rows.
CREATE TRIGGER IF NOT EXISTS trg_receipt_items_fact_set_no_insert_collision
    BEFORE INSERT ON receipt_items
    WHEN EXISTS (
        SELECT 1
        FROM receipt_items AS existing
        WHERE (
            existing.id = NEW.id
            OR existing.public_id = NEW.public_id
            OR (
                NEW.fact_set_id IS NOT NULL
                AND existing.fact_set_id = NEW.fact_set_id
                AND existing.line_number = NEW.line_number
            )
        )
        AND (
            existing.fact_set_id IS NOT NULL
            OR NEW.fact_set_id IS NOT NULL
        )
    )
BEGIN
    SELECT RAISE(
        ABORT,
        'UNIQUE receipt item identity collision: fact-set-bound receipt_items rows cannot be replaced'
    );
END;

-- UPDATE OR REPLACE protection: an update of a legacy row whose NEW
-- identity collides with a different bound row would REPLACE-delete the
-- bound row without firing its DELETE trigger.  Plain BEFORE UPDATE, so
-- rowid-alias collisions are covered too.
CREATE TRIGGER IF NOT EXISTS trg_receipt_items_fact_set_no_update_collision
    BEFORE UPDATE ON receipt_items
    FOR EACH ROW
    WHEN EXISTS (
        SELECT 1
        FROM receipt_items AS existing
        WHERE existing.id <> OLD.id
        AND (
            existing.id = NEW.id
            OR existing.public_id = NEW.public_id
            OR (
                NEW.fact_set_id IS NOT NULL
                AND existing.fact_set_id = NEW.fact_set_id
                AND existing.line_number = NEW.line_number
            )
        )
        AND (
            existing.fact_set_id IS NOT NULL
            OR NEW.fact_set_id IS NOT NULL
        )
    )
BEGIN
    SELECT RAISE(
        ABORT,
        'UNIQUE receipt item identity collision: fact-set-bound receipt_items rows cannot be replaced'
    );
END;

-- -----------------------------------------------------------------------
-- receipt_adjustments additive extension (Section 8)
-- -----------------------------------------------------------------------

-- Explicit semantic adjustment order (1..N).  Contiguity is a service
-- obligation (IAF.2); the schema backstops positivity and per-fact-set
-- uniqueness.
ALTER TABLE receipt_adjustments ADD COLUMN adjustment_index INTEGER CHECK (
    adjustment_index IS NULL
    OR adjustment_index >= 1
);

-- Authoritative canonical adjustment amount (strictly positive; the
-- direction column carries the sign semantics, Section 8.2).
ALTER TABLE receipt_adjustments ADD COLUMN amount_canonical_text TEXT CHECK (
    amount_canonical_text IS NULL
    OR CASE
        WHEN currency IN ('SGD', 'USD', 'EUR', 'GBP', 'AUD', 'CNY', 'HKD') THEN (
            amount_canonical_text NOT GLOB '*[^0-9.]*'
            AND amount_canonical_text GLOB '*.[0-9][0-9]'
            AND instr(amount_canonical_text, '.')
                = length(amount_canonical_text) - 2
            AND (
                amount_canonical_text GLOB '[1-9]*'
                OR (
                    substr(amount_canonical_text, 1, 1) = '0'
                    AND length(amount_canonical_text) = 4
                )
            )
            AND amount_canonical_text <> '0.00'
        )
        WHEN currency = 'JPY' THEN (
            amount_canonical_text NOT GLOB '*[^0-9]*'
            AND amount_canonical_text GLOB '[1-9]*'
        )
        ELSE 0
    END
);

-- Fact-set binding for adjustments.  A bound row must carry the explicit
-- index and canonical amount, stay inside the approved IAF v1 direction
-- and allocation-method vocabulary (IA-D7b: informational, excluded and
-- proportional_by_net_amount are rejected for bound rows; unbound legacy
-- rows keep the full migration 002 vocabulary), and any description must
-- be nonblank and at most 500 code points.  SQLite length() counts code
-- points (matching Python len()); trim() strips ASCII spaces only, so the
-- full Python str.strip() whitespace semantics remain a service
-- obligation - this CHECK is a backstop, not a duplicate.
ALTER TABLE receipt_adjustments ADD COLUMN fact_set_id TEXT
    REFERENCES receipt_item_allocation_fact_sets(fact_set_public_id)
    CHECK (
        fact_set_id IS NULL
        OR (
            substr(fact_set_id, 1, 4) = 'rfs_'
            AND length(fact_set_id) BETWEEN 5 AND 200
            AND fact_set_id NOT GLOB '*[^A-Za-z0-9_-]*'
            AND adjustment_index IS NOT NULL
            AND amount_canonical_text IS NOT NULL
            AND direction IN ('add', 'subtract')
            AND allocation_method IN (
                'equal_per_participant',
                'proportional_by_item_amount',
                'manual',
                'payer_only'
            )
            AND (
                description IS NULL
                OR (
                    length(trim(description)) > 0
                    AND length(description) <= 500
                )
            )
        )
    );

-- Fact-set-bound adjustment indices are unique within their fact set.
CREATE UNIQUE INDEX IF NOT EXISTS idx_receipt_adjustments_fact_set_index
    ON receipt_adjustments(fact_set_id, adjustment_index)
    WHERE fact_set_id IS NOT NULL;

-- Bound adjustment receipt identity must agree with the registry receipt
-- (and the registry row must exist, independent of foreign-key pragma).
CREATE TRIGGER IF NOT EXISTS trg_receipt_adjustments_fact_set_receipt_match
    BEFORE INSERT ON receipt_adjustments
    WHEN NEW.fact_set_id IS NOT NULL
        AND NOT EXISTS (
            SELECT 1
            FROM receipt_item_allocation_fact_sets
            WHERE fact_set_public_id = NEW.fact_set_id
              AND receipt_id = NEW.receipt_id
        )
BEGIN
    SELECT RAISE(
        ABORT,
        'fact-set-bound receipt_adjustments rows must reference a fact set registry row for the same receipt'
    );
END;

CREATE TRIGGER IF NOT EXISTS trg_receipt_adjustments_fact_set_bound_freeze
    BEFORE UPDATE ON receipt_adjustments
    FOR EACH ROW
    WHEN OLD.fact_set_id IS NOT NULL
        OR NEW.fact_set_id IS NOT NULL
BEGIN
    SELECT RAISE(
        ABORT,
        'fact-set-bound receipt_adjustments rows are append-only and legacy rows cannot be adopted into a fact set'
    );
END;

CREATE TRIGGER IF NOT EXISTS trg_receipt_adjustments_fact_set_bound_no_delete
    BEFORE DELETE ON receipt_adjustments
    WHEN OLD.fact_set_id IS NOT NULL
BEGIN
    SELECT RAISE(
        ABORT,
        'fact-set-bound receipt_adjustments rows cannot be deleted'
    );
END;

CREATE TRIGGER IF NOT EXISTS trg_receipt_adjustments_fact_set_no_insert_collision
    BEFORE INSERT ON receipt_adjustments
    WHEN EXISTS (
        SELECT 1
        FROM receipt_adjustments AS existing
        WHERE (
            existing.id = NEW.id
            OR existing.public_id = NEW.public_id
            OR (
                NEW.fact_set_id IS NOT NULL
                AND existing.fact_set_id = NEW.fact_set_id
                AND existing.adjustment_index = NEW.adjustment_index
            )
        )
        AND (
            existing.fact_set_id IS NOT NULL
            OR NEW.fact_set_id IS NOT NULL
        )
    )
BEGIN
    SELECT RAISE(
        ABORT,
        'UNIQUE receipt adjustment identity collision: fact-set-bound receipt_adjustments rows cannot be replaced'
    );
END;

CREATE TRIGGER IF NOT EXISTS trg_receipt_adjustments_fact_set_no_update_collision
    BEFORE UPDATE ON receipt_adjustments
    FOR EACH ROW
    WHEN EXISTS (
        SELECT 1
        FROM receipt_adjustments AS existing
        WHERE existing.id <> OLD.id
        AND (
            existing.id = NEW.id
            OR existing.public_id = NEW.public_id
            OR (
                NEW.fact_set_id IS NOT NULL
                AND existing.fact_set_id = NEW.fact_set_id
                AND existing.adjustment_index = NEW.adjustment_index
            )
        )
        AND (
            existing.fact_set_id IS NOT NULL
            OR NEW.fact_set_id IS NOT NULL
        )
    )
BEGIN
    SELECT RAISE(
        ABORT,
        'UNIQUE receipt adjustment identity collision: fact-set-bound receipt_adjustments rows cannot be replaced'
    );
END;

-- -----------------------------------------------------------------------
-- Allocation fact table (Section 10.1 hybrid model)
-- -----------------------------------------------------------------------

-- Legacy receipt_item_allocations is NOT written by this boundary: its
-- share_amount_before_service_charge column is NOT NULL, but equal_amount
-- allocations deliberately persist no per-participant amount (a
-- pre-rounded equal share would be a fabricated monetary fact).  The
-- legacy table remains untouched for pre-existing TC001/seed data.
--
-- share_amount_canonical_text carries a generic strictly-positive
-- canonical decimal shape backstop only: this table has no currency
-- column (currency lives on the receipt/item), so exact minor-unit scale
-- validation per currency remains a service-level Money Contract
-- obligation (IAF.2), not a schema claim.
CREATE TABLE IF NOT EXISTS receipt_item_allocation_facts (
    allocation_public_id TEXT PRIMARY KEY NOT NULL CHECK (
        substr(allocation_public_id, 1, 5) = 'rfsa_'
        AND length(allocation_public_id) BETWEEN 6 AND 200
        AND allocation_public_id NOT GLOB '*[^A-Za-z0-9_-]*'
    ),
    fact_set_id TEXT NOT NULL CHECK (
        substr(fact_set_id, 1, 4) = 'rfs_'
        AND length(fact_set_id) BETWEEN 5 AND 200
        AND fact_set_id NOT GLOB '*[^A-Za-z0-9_-]*'
    ),
    receipt_item_id INTEGER NOT NULL,
    participant_id INTEGER NOT NULL,
    allocation_method TEXT NOT NULL CHECK (
        allocation_method IN ('equal_amount', 'manual')
    ),
    share_amount_canonical_text TEXT,
    -- NUMERIC compatibility mirror; NULL exactly when the canonical text
    -- is NULL (equal_amount persists no per-participant amount).
    share_amount NUMERIC,
    schema_version TEXT NOT NULL DEFAULT 'v1' CHECK (schema_version = 'v1'),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (fact_set_id)
        REFERENCES receipt_item_allocation_fact_sets(fact_set_public_id),
    FOREIGN KEY (receipt_item_id) REFERENCES receipt_items(id),
    FOREIGN KEY (participant_id) REFERENCES participants(id),
    UNIQUE (receipt_item_id, participant_id),
    -- manual requires an explicit canonical share; equal_amount forbids
    -- one (no fabricated equal-split amounts, Section 7).
    CHECK (
        (allocation_method = 'manual' AND share_amount_canonical_text IS NOT NULL)
        OR (
            allocation_method = 'equal_amount'
            AND share_amount_canonical_text IS NULL
        )
    ),
    CHECK ((share_amount IS NULL) = (share_amount_canonical_text IS NULL)),
    -- Generic strictly-positive canonical decimal shape: digits with at
    -- most one interior dot, no sign/exponent/whitespace, no redundant
    -- leading zeros, and at least one nonzero digit.
    CHECK (
        share_amount_canonical_text IS NULL
        OR (
            share_amount_canonical_text NOT GLOB '*[^0-9.]*'
            AND share_amount_canonical_text NOT GLOB '*.*.*'
            AND share_amount_canonical_text NOT GLOB '.*'
            AND share_amount_canonical_text NOT GLOB '*.'
            AND (
                share_amount_canonical_text GLOB '[1-9]*'
                OR share_amount_canonical_text GLOB '0.*'
            )
            AND share_amount_canonical_text GLOB '*[1-9]*'
        )
    )
);

-- Fact-set/item consistency at schema level: every allocation fact must
-- reference a receipt_items row bound to the same fact set.  A bound item
-- implies the registry row exists, so this is also FK-pragma-independent.
CREATE TRIGGER IF NOT EXISTS trg_receipt_item_allocation_facts_require_bound_item
    BEFORE INSERT ON receipt_item_allocation_facts
    WHEN NOT EXISTS (
        SELECT 1
        FROM receipt_items
        WHERE id = NEW.receipt_item_id
          AND fact_set_id = NEW.fact_set_id
    )
BEGIN
    SELECT RAISE(
        ABORT,
        'allocation facts must reference a receipt_items row bound to the same fact set'
    );
END;

CREATE TRIGGER IF NOT EXISTS trg_receipt_item_allocation_facts_no_update
    BEFORE UPDATE ON receipt_item_allocation_facts
BEGIN
    SELECT RAISE(
        ABORT,
        'receipt_item_allocation_facts rows are append-only'
    );
END;

CREATE TRIGGER IF NOT EXISTS trg_receipt_item_allocation_facts_no_delete
    BEFORE DELETE ON receipt_item_allocation_facts
BEGIN
    SELECT RAISE(
        ABORT,
        'receipt_item_allocation_facts rows are append-only'
    );
END;

-- REPLACE protection on both allocation identities (public ID and the
-- item/participant pair), firing before conflict resolution.
CREATE TRIGGER IF NOT EXISTS trg_receipt_item_allocation_facts_no_insert_collision
    BEFORE INSERT ON receipt_item_allocation_facts
    WHEN EXISTS (
        SELECT 1
        FROM receipt_item_allocation_facts AS existing
        WHERE existing.allocation_public_id = NEW.allocation_public_id
           OR (
               existing.receipt_item_id = NEW.receipt_item_id
               AND existing.participant_id = NEW.participant_id
           )
    )
BEGIN
    SELECT RAISE(
        ABORT,
        'UNIQUE allocation fact identity collision: receipt_item_allocation_facts rows are append-only and cannot be replaced'
    );
END;
