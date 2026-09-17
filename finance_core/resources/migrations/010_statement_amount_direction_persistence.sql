PRAGMA foreign_keys = OFF;

-- Statement Amount Direction Persistence v1
-- Persists amount_direction, raw_amount, and raw_amount_type from
-- StatementAmountDirection classification (PR #70) into the
-- statement_transactions table.
--
-- Nullable columns. No existing data altered. No backfill.
-- CHECK constraint at Python layer only (SQLite ALTER TABLE limitation).

ALTER TABLE statement_transactions
ADD COLUMN amount_direction TEXT;

ALTER TABLE statement_transactions
ADD COLUMN raw_amount TEXT;

ALTER TABLE statement_transactions
ADD COLUMN raw_amount_type TEXT;

CREATE INDEX IF NOT EXISTS idx_stmt_txns_amount_direction
  ON statement_transactions(amount_direction)
  WHERE amount_direction IS NOT NULL;

PRAGMA foreign_keys = ON;
