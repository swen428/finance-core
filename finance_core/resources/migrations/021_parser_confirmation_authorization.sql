PRAGMA foreign_keys = ON;

-- Authoritative parser-proposal confirmation records.  Migration 004 remains
-- immutable legacy audit history: its rows are deliberately not trusted for
-- financial conversion.
CREATE TABLE IF NOT EXISTS parser_proposal_authorizations (
    confirmation_public_id TEXT PRIMARY KEY CHECK (length(trim(confirmation_public_id)) > 0),
    parser_output_id INTEGER NOT NULL UNIQUE,
    proposal_content_hash TEXT NOT NULL CHECK (
        length(proposal_content_hash) = 64
        AND proposal_content_hash NOT GLOB '*[^0-9a-f]*'
    ),
    actor_type TEXT NOT NULL CHECK (actor_type = 'human'),
    authenticated_actor_id TEXT NOT NULL CHECK (length(trim(authenticated_actor_id)) > 0),
    confirmation_state TEXT NOT NULL CHECK (
        confirmation_state IN ('confirmed', 'rejected', 'revoked')
    ),
    confirmation_channel TEXT NOT NULL CHECK (length(trim(confirmation_channel)) > 0),
    decided_at TEXT NOT NULL,
    revoked_at TEXT,
    schema_version TEXT NOT NULL DEFAULT 'v1' CHECK (schema_version = 'v1'),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (parser_output_id) REFERENCES parser_outputs(id),
    CHECK (
        (confirmation_state = 'revoked' AND revoked_at IS NOT NULL)
        OR (confirmation_state != 'revoked' AND revoked_at IS NULL)
    )
);

CREATE INDEX IF NOT EXISTS idx_parser_proposal_authorizations_output_state
    ON parser_proposal_authorizations(parser_output_id, confirmation_state);
CREATE INDEX IF NOT EXISTS idx_parser_proposal_authorizations_hash
    ON parser_proposal_authorizations(proposal_content_hash);

-- A successful conversion is inseparable from its canonical transaction.
-- There is at most one canonical conversion per proposal and confirmation.
CREATE TABLE IF NOT EXISTS parser_proposal_conversion_audit (
    id INTEGER PRIMARY KEY,
    parser_output_id INTEGER NOT NULL UNIQUE,
    transaction_id INTEGER NOT NULL UNIQUE,
    confirmation_public_id TEXT NOT NULL UNIQUE,
    proposal_content_hash TEXT NOT NULL CHECK (
        length(proposal_content_hash) = 64
        AND proposal_content_hash NOT GLOB '*[^0-9a-f]*'
    ),
    authenticated_actor_id TEXT NOT NULL CHECK (length(trim(authenticated_actor_id)) > 0),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (parser_output_id) REFERENCES parser_outputs(id),
    FOREIGN KEY (transaction_id) REFERENCES transactions(id),
    FOREIGN KEY (confirmation_public_id)
        REFERENCES parser_proposal_authorizations(confirmation_public_id)
);

CREATE INDEX IF NOT EXISTS idx_parser_proposal_conversion_audit_transaction
    ON parser_proposal_conversion_audit(transaction_id);
