PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS accounts (
  id INTEGER PRIMARY KEY,
  public_id TEXT NOT NULL UNIQUE,
  account_name TEXT NOT NULL,
  account_type TEXT NOT NULL,
  institution TEXT,
  default_currency TEXT NOT NULL,
  country TEXT,
  is_active INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
  notes TEXT,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS participants (
  id INTEGER PRIMARY KEY,
  public_id TEXT NOT NULL UNIQUE,
  display_name TEXT NOT NULL,
  aliases TEXT,
  telegram_user_id TEXT,
  is_self INTEGER NOT NULL DEFAULT 0 CHECK (is_self IN (0, 1)),
  is_active INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
  notes TEXT,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS statement_batches (
  id INTEGER PRIMARY KEY,
  public_id TEXT NOT NULL UNIQUE,
  statement_source TEXT NOT NULL,
  source_file_path TEXT,
  source_file_hash TEXT,
  source_file_type TEXT,
  institution TEXT,
  platform TEXT,
  account_id INTEGER,
  statement_type TEXT,
  period_start TEXT,
  period_end TEXT,
  imported_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  import_status TEXT NOT NULL DEFAULT 'imported',
  reprocessing_status TEXT,
  parser_name TEXT,
  parser_version TEXT,
  ai_provider TEXT,
  ai_model TEXT,
  notes TEXT,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  FOREIGN KEY (account_id) REFERENCES accounts(id)
);

CREATE TABLE IF NOT EXISTS parser_outputs (
  id INTEGER PRIMARY KEY,
  public_id TEXT NOT NULL UNIQUE,
  source_type TEXT NOT NULL,
  source_public_id TEXT,
  statement_batch_id INTEGER,
  attachment_id INTEGER,
  parser_name TEXT,
  parser_version TEXT,
  ai_provider TEXT,
  ai_model TEXT,
  prompt_version TEXT,
  raw_text TEXT,
  parsed_payload TEXT,
  normalized_payload TEXT,
  confidence_score REAL,
  parse_status TEXT NOT NULL DEFAULT 'parsed',
  parent_parser_output_id INTEGER,
  reprocessed_at TEXT,
  notes TEXT,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  FOREIGN KEY (statement_batch_id) REFERENCES statement_batches(id),
  FOREIGN KEY (attachment_id) REFERENCES attachments(id),
  FOREIGN KEY (parent_parser_output_id) REFERENCES parser_outputs(id)
);

CREATE TABLE IF NOT EXISTS transactions (
  id INTEGER PRIMARY KEY,
  public_id TEXT NOT NULL UNIQUE,
  intent TEXT NOT NULL,
  intent_type TEXT NOT NULL CHECK (intent_type IN ('Manual', 'Generated')),
  source_channel TEXT,
  transaction_date TEXT NOT NULL,
  posted_date TEXT,
  status TEXT NOT NULL DEFAULT 'active',
  review_status TEXT,
  account_id INTEGER,
  from_account_id INTEGER,
  to_account_id INTEGER,
  investment_account_id INTEGER,
  paid_by_participant_id INTEGER,
  from_participant_id INTEGER,
  to_participant_id INTEGER,
  amount NUMERIC,
  total_amount NUMERIC,
  currency TEXT,
  posted_currency TEXT,
  from_currency TEXT,
  to_currency TEXT,
  from_amount NUMERIC,
  to_amount NUMERIC,
  exchange_rate NUMERIC,
  fee_amount NUMERIC,
  fee_currency TEXT,
  fee_type TEXT,
  merchant TEXT,
  category TEXT,
  source_name TEXT,
  platform TEXT,
  asset_name TEXT,
  asset_symbol TEXT,
  quantity NUMERIC,
  unit_price NUMERIC,
  withholding_tax_amount NUMERIC,
  adjustment_type TEXT,
  split_type TEXT,
  trip TEXT,
  project TEXT,
  notes TEXT,
  raw_input TEXT,
  statement_source TEXT,
  telegram_message_id TEXT,
  statement_batch_id INTEGER,
  parser_output_id INTEGER,
  confidence_score REAL,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  FOREIGN KEY (account_id) REFERENCES accounts(id),
  FOREIGN KEY (from_account_id) REFERENCES accounts(id),
  FOREIGN KEY (to_account_id) REFERENCES accounts(id),
  FOREIGN KEY (investment_account_id) REFERENCES accounts(id),
  FOREIGN KEY (paid_by_participant_id) REFERENCES participants(id),
  FOREIGN KEY (from_participant_id) REFERENCES participants(id),
  FOREIGN KEY (to_participant_id) REFERENCES participants(id),
  FOREIGN KEY (statement_batch_id) REFERENCES statement_batches(id),
  FOREIGN KEY (parser_output_id) REFERENCES parser_outputs(id)
);

CREATE TABLE IF NOT EXISTS attachments (
  id INTEGER PRIMARY KEY,
  public_id TEXT NOT NULL UNIQUE,
  attachment_type TEXT NOT NULL,
  file_path TEXT NOT NULL,
  original_filename TEXT,
  mime_type TEXT,
  file_hash TEXT,
  source_channel TEXT,
  transaction_id INTEGER,
  statement_batch_id INTEGER,
  parser_output_id INTEGER,
  notes TEXT,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  FOREIGN KEY (transaction_id) REFERENCES transactions(id),
  FOREIGN KEY (statement_batch_id) REFERENCES statement_batches(id),
  FOREIGN KEY (parser_output_id) REFERENCES parser_outputs(id)
);

CREATE TABLE IF NOT EXISTS shared_expense_obligations (
  id INTEGER PRIMARY KEY,
  public_id TEXT NOT NULL UNIQUE,
  shared_expense_transaction_id INTEGER NOT NULL,
  participant_id INTEGER NOT NULL,
  owed_to_participant_id INTEGER,
  share_amount NUMERIC NOT NULL,
  currency TEXT NOT NULL,
  split_type TEXT,
  split_ratio NUMERIC,
  is_excluded INTEGER NOT NULL DEFAULT 0 CHECK (is_excluded IN (0, 1)),
  status TEXT NOT NULL DEFAULT 'outstanding',
  notes TEXT,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  FOREIGN KEY (shared_expense_transaction_id) REFERENCES transactions(id),
  FOREIGN KEY (participant_id) REFERENCES participants(id),
  FOREIGN KEY (owed_to_participant_id) REFERENCES participants(id)
);

CREATE TABLE IF NOT EXISTS transaction_links (
  id INTEGER PRIMARY KEY,
  public_id TEXT NOT NULL UNIQUE,
  source_transaction_id INTEGER NOT NULL,
  target_transaction_id INTEGER NOT NULL,
  link_type TEXT NOT NULL,
  amount NUMERIC,
  currency TEXT,
  status TEXT NOT NULL DEFAULT 'active',
  notes TEXT,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  FOREIGN KEY (source_transaction_id) REFERENCES transactions(id),
  FOREIGN KEY (target_transaction_id) REFERENCES transactions(id)
);

CREATE TABLE IF NOT EXISTS reconciliation_records (
  id INTEGER PRIMARY KEY,
  public_id TEXT NOT NULL UNIQUE,
  manual_transaction_id INTEGER,
  generated_transaction_id INTEGER,
  statement_batch_id INTEGER,
  match_status TEXT NOT NULL,
  match_method TEXT,
  confidence_score REAL,
  mismatch_reason TEXT,
  mismatch_details TEXT,
  review_status TEXT NOT NULL DEFAULT 'unreviewed',
  reviewer_notes TEXT,
  decided_at TEXT,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  FOREIGN KEY (manual_transaction_id) REFERENCES transactions(id),
  FOREIGN KEY (generated_transaction_id) REFERENCES transactions(id),
  FOREIGN KEY (statement_batch_id) REFERENCES statement_batches(id)
);

CREATE INDEX IF NOT EXISTS idx_accounts_type ON accounts(account_type);
CREATE INDEX IF NOT EXISTS idx_accounts_active ON accounts(is_active);

CREATE INDEX IF NOT EXISTS idx_participants_display_name ON participants(display_name);
CREATE INDEX IF NOT EXISTS idx_participants_is_self ON participants(is_self);
CREATE INDEX IF NOT EXISTS idx_participants_active ON participants(is_active);

CREATE INDEX IF NOT EXISTS idx_transactions_date ON transactions(transaction_date);
CREATE INDEX IF NOT EXISTS idx_transactions_intent ON transactions(intent);
CREATE INDEX IF NOT EXISTS idx_transactions_intent_type ON transactions(intent_type);
CREATE INDEX IF NOT EXISTS idx_transactions_status ON transactions(status);
CREATE INDEX IF NOT EXISTS idx_transactions_account_id ON transactions(account_id);
CREATE INDEX IF NOT EXISTS idx_transactions_from_account_id ON transactions(from_account_id);
CREATE INDEX IF NOT EXISTS idx_transactions_to_account_id ON transactions(to_account_id);
CREATE INDEX IF NOT EXISTS idx_transactions_investment_account_id ON transactions(investment_account_id);
CREATE INDEX IF NOT EXISTS idx_transactions_paid_by_participant_id ON transactions(paid_by_participant_id);
CREATE INDEX IF NOT EXISTS idx_transactions_from_participant_id ON transactions(from_participant_id);
CREATE INDEX IF NOT EXISTS idx_transactions_to_participant_id ON transactions(to_participant_id);
CREATE INDEX IF NOT EXISTS idx_transactions_statement_batch_id ON transactions(statement_batch_id);
CREATE INDEX IF NOT EXISTS idx_transactions_parser_output_id ON transactions(parser_output_id);
CREATE INDEX IF NOT EXISTS idx_transactions_statement_source ON transactions(statement_source);
CREATE INDEX IF NOT EXISTS idx_transactions_asset_symbol ON transactions(asset_symbol);
CREATE INDEX IF NOT EXISTS idx_transactions_trip ON transactions(trip);
CREATE INDEX IF NOT EXISTS idx_transactions_project ON transactions(project);

CREATE INDEX IF NOT EXISTS idx_obligations_shared_expense ON shared_expense_obligations(shared_expense_transaction_id);
CREATE INDEX IF NOT EXISTS idx_obligations_participant ON shared_expense_obligations(participant_id);
CREATE INDEX IF NOT EXISTS idx_obligations_owed_to ON shared_expense_obligations(owed_to_participant_id);
CREATE INDEX IF NOT EXISTS idx_obligations_status ON shared_expense_obligations(status);

CREATE INDEX IF NOT EXISTS idx_transaction_links_source ON transaction_links(source_transaction_id);
CREATE INDEX IF NOT EXISTS idx_transaction_links_target ON transaction_links(target_transaction_id);
CREATE INDEX IF NOT EXISTS idx_transaction_links_type ON transaction_links(link_type);
CREATE INDEX IF NOT EXISTS idx_transaction_links_status ON transaction_links(status);

CREATE INDEX IF NOT EXISTS idx_attachments_transaction_id ON attachments(transaction_id);
CREATE INDEX IF NOT EXISTS idx_attachments_statement_batch_id ON attachments(statement_batch_id);
CREATE INDEX IF NOT EXISTS idx_attachments_parser_output_id ON attachments(parser_output_id);
CREATE INDEX IF NOT EXISTS idx_attachments_file_hash ON attachments(file_hash);

CREATE INDEX IF NOT EXISTS idx_statement_batches_account_id ON statement_batches(account_id);
CREATE INDEX IF NOT EXISTS idx_statement_batches_source ON statement_batches(statement_source);
CREATE INDEX IF NOT EXISTS idx_statement_batches_file_hash ON statement_batches(source_file_hash);
CREATE INDEX IF NOT EXISTS idx_statement_batches_status ON statement_batches(import_status);
CREATE INDEX IF NOT EXISTS idx_statement_batches_parser_version ON statement_batches(parser_version);

CREATE INDEX IF NOT EXISTS idx_parser_outputs_statement_batch_id ON parser_outputs(statement_batch_id);
CREATE INDEX IF NOT EXISTS idx_parser_outputs_attachment_id ON parser_outputs(attachment_id);
CREATE INDEX IF NOT EXISTS idx_parser_outputs_parent_id ON parser_outputs(parent_parser_output_id);
CREATE INDEX IF NOT EXISTS idx_parser_outputs_status ON parser_outputs(parse_status);
CREATE INDEX IF NOT EXISTS idx_parser_outputs_parser_version ON parser_outputs(parser_version);

CREATE INDEX IF NOT EXISTS idx_reconciliation_manual_transaction_id ON reconciliation_records(manual_transaction_id);
CREATE INDEX IF NOT EXISTS idx_reconciliation_generated_transaction_id ON reconciliation_records(generated_transaction_id);
CREATE INDEX IF NOT EXISTS idx_reconciliation_statement_batch_id ON reconciliation_records(statement_batch_id);
CREATE INDEX IF NOT EXISTS idx_reconciliation_match_status ON reconciliation_records(match_status);
CREATE INDEX IF NOT EXISTS idx_reconciliation_review_status ON reconciliation_records(review_status);

-- Validation guidance:
-- This migration defines the core schema. Validate schema changes against
-- temporary/test SQLite databases, not database/finance.db or live data.
-- Tests must not modify database/finance.db.
-- Do not seed or change live database data unless Wen explicitly requests it.
