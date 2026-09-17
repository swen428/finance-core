PRAGMA foreign_keys = OFF;

-- Reconciliation Persistence Schema v1
-- Persists structured statement import batches, statement transaction rows,
-- reconciliation runs, and match results with audit evidence.
--
-- This migration does not implement PDF parsing, OCR, Telegram integration,
-- Metabase dashboards, or repository runtime. It provides the persistence
-- foundation that future layers require.

CREATE TABLE IF NOT EXISTS statement_import_batches (
  id INTEGER PRIMARY KEY,
  public_id TEXT NOT NULL UNIQUE,
  source_type TEXT NOT NULL CHECK (
    source_type IN (
      'bank_statement',
      'credit_card_statement',
      'structured_csv',
      'manual_test_fixture'
    )
  ),
  account_id INTEGER,
  account_name TEXT,
  statement_period_start TEXT,
  statement_period_end TEXT,
  currency TEXT,
  source_file_path TEXT,
  source_file_hash TEXT,
  imported_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  FOREIGN KEY (account_id) REFERENCES accounts(id)
);

CREATE TABLE IF NOT EXISTS statement_transactions (
  id INTEGER PRIMARY KEY,
  public_id TEXT NOT NULL UNIQUE,
  batch_id INTEGER NOT NULL,
  transaction_date TEXT,
  posted_date TEXT,
  merchant_raw TEXT NOT NULL,
  merchant_normalized TEXT,
  amount NUMERIC NOT NULL CHECK (amount > 0),
  currency TEXT NOT NULL,
  account_id INTEGER,
  account_name TEXT,
  statement_row_reference TEXT,
  raw_row_payload_json TEXT,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  FOREIGN KEY (batch_id) REFERENCES statement_import_batches(id),
  FOREIGN KEY (account_id) REFERENCES accounts(id)
);

CREATE TABLE IF NOT EXISTS reconciliation_runs (
  id INTEGER PRIMARY KEY,
  public_id TEXT NOT NULL UNIQUE,
  batch_id INTEGER,
  run_status TEXT NOT NULL CHECK (
    run_status IN ('started', 'completed', 'failed', 'cancelled')
  ),
  matcher_version TEXT,
  started_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  completed_at TEXT,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  FOREIGN KEY (batch_id) REFERENCES statement_import_batches(id)
);

CREATE TABLE IF NOT EXISTS reconciliation_match_results (
  id INTEGER PRIMARY KEY,
  public_id TEXT NOT NULL UNIQUE,
  run_id INTEGER NOT NULL,
  statement_transaction_id INTEGER NOT NULL,
  internal_candidate_id TEXT,
  match_status TEXT NOT NULL CHECK (
    match_status IN (
      'matched',
      'no_match',
      'amount_mismatch',
      'currency_mismatch',
      'date_mismatch',
      'merchant_mismatch',
      'possible_duplicate',
      'ambiguous',
      'needs_review'
    )
  ),
  reason_codes_json TEXT NOT NULL DEFAULT '[]',
  evidence_json TEXT NOT NULL DEFAULT '{}',
  amount_delta NUMERIC,
  date_delta_days INTEGER,
  merchant_similarity REAL,
  needs_review INTEGER NOT NULL DEFAULT 0 CHECK (needs_review IN (0, 1)),
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  FOREIGN KEY (run_id) REFERENCES reconciliation_runs(id),
  FOREIGN KEY (statement_transaction_id) REFERENCES statement_transactions(id)
);

-- statement_import_batches indexes
CREATE INDEX IF NOT EXISTS idx_import_batches_source_type
  ON statement_import_batches(source_type);
CREATE INDEX IF NOT EXISTS idx_import_batches_account_id
  ON statement_import_batches(account_id);
CREATE INDEX IF NOT EXISTS idx_import_batches_file_hash
  ON statement_import_batches(source_file_hash)
  WHERE source_file_hash IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_import_batches_period
  ON statement_import_batches(statement_period_start, statement_period_end)
  WHERE statement_period_start IS NOT NULL;

-- statement_transactions indexes
CREATE INDEX IF NOT EXISTS idx_stmt_txns_batch_id
  ON statement_transactions(batch_id);
CREATE INDEX IF NOT EXISTS idx_stmt_txns_transaction_date
  ON statement_transactions(transaction_date)
  WHERE transaction_date IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_stmt_txns_posted_date
  ON statement_transactions(posted_date)
  WHERE posted_date IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_stmt_txns_amount_currency
  ON statement_transactions(amount, currency);
CREATE INDEX IF NOT EXISTS idx_stmt_txns_account_id
  ON statement_transactions(account_id);

-- reconciliation_runs indexes
CREATE INDEX IF NOT EXISTS idx_recon_runs_batch_id
  ON reconciliation_runs(batch_id);
CREATE INDEX IF NOT EXISTS idx_recon_runs_status
  ON reconciliation_runs(run_status);

-- reconciliation_match_results indexes
CREATE INDEX IF NOT EXISTS idx_match_results_run_id
  ON reconciliation_match_results(run_id);
CREATE INDEX IF NOT EXISTS idx_match_results_stmt_txn_id
  ON reconciliation_match_results(statement_transaction_id);
CREATE INDEX IF NOT EXISTS idx_match_results_internal_candidate_id
  ON reconciliation_match_results(internal_candidate_id)
  WHERE internal_candidate_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_match_results_match_status
  ON reconciliation_match_results(match_status);
CREATE INDEX IF NOT EXISTS idx_match_results_needs_review
  ON reconciliation_match_results(needs_review)
  WHERE needs_review = 1;

PRAGMA foreign_keys = ON;

-- Validation guidance:
-- This migration defines the reconciliation persistence schema v1.
-- Validate schema changes against temporary/test SQLite databases,
-- not database/finance.db or live data.
-- Tests must not modify database/finance.db.
-- Do not seed or change live database data unless Wen explicitly requests it.
