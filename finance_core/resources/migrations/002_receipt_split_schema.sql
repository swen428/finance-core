PRAGMA foreign_keys = ON;

-- Receipt splitting layer.
-- This migration intentionally adds tables instead of replacing the v1 core
-- transaction and shared_expense_obligations tables.

CREATE TABLE IF NOT EXISTS receipt_groups (
  id INTEGER PRIMARY KEY,
  public_id TEXT NOT NULL UNIQUE,
  group_name TEXT,
  group_type TEXT NOT NULL DEFAULT 'shared_expense'
    CHECK (group_type IN ('shared_expense', 'trip', 'event', 'household', 'manual')),
  currency TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'active'
    CHECK (status IN ('active', 'calculated', 'settled', 'cancelled', 'needs_review')),
  source TEXT,
  raw_input TEXT,
  notes TEXT,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS receipts (
  id INTEGER PRIMARY KEY,
  public_id TEXT NOT NULL UNIQUE,
  transaction_id INTEGER,
  merchant TEXT NOT NULL,
  receipt_datetime TEXT,
  gross_amount NUMERIC,
  subtotal_amount NUMERIC,
  service_charge_amount NUMERIC,
  tax_amount NUMERIC,
  discount_amount NUMERIC,
  net_paid_amount NUMERIC NOT NULL,
  currency TEXT NOT NULL,
  payer_participant_id INTEGER NOT NULL,
  source_channel TEXT,
  raw_input TEXT,
  attachment_id INTEGER,
  attachment_path TEXT,
  payment_record_attachment_id INTEGER,
  ocr_confidence REAL CHECK (ocr_confidence IS NULL OR (ocr_confidence >= 0 AND ocr_confidence <= 1)),
  parser_output_id INTEGER,
  status TEXT NOT NULL DEFAULT 'confirmed'
    CHECK (status IN ('draft', 'needs_review', 'confirmed', 'voided')),
  notes TEXT,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  FOREIGN KEY (transaction_id) REFERENCES transactions(id),
  FOREIGN KEY (payer_participant_id) REFERENCES participants(id),
  FOREIGN KEY (attachment_id) REFERENCES attachments(id),
  FOREIGN KEY (payment_record_attachment_id) REFERENCES attachments(id),
  FOREIGN KEY (parser_output_id) REFERENCES parser_outputs(id)
);

CREATE TABLE IF NOT EXISTS receipt_group_receipts (
  id INTEGER PRIMARY KEY,
  public_id TEXT NOT NULL UNIQUE,
  receipt_group_id INTEGER NOT NULL,
  receipt_id INTEGER NOT NULL,
  sequence_number INTEGER,
  notes TEXT,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  FOREIGN KEY (receipt_group_id) REFERENCES receipt_groups(id),
  FOREIGN KEY (receipt_id) REFERENCES receipts(id),
  UNIQUE (receipt_group_id, receipt_id)
);

CREATE TABLE IF NOT EXISTS receipt_participants (
  id INTEGER PRIMARY KEY,
  public_id TEXT NOT NULL UNIQUE,
  receipt_id INTEGER NOT NULL,
  participant_id INTEGER NOT NULL,
  role TEXT NOT NULL DEFAULT 'participant'
    CHECK (role IN ('payer', 'participant', 'excluded', 'observer')),
  is_included INTEGER NOT NULL DEFAULT 1 CHECK (is_included IN (0, 1)),
  notes TEXT,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  FOREIGN KEY (receipt_id) REFERENCES receipts(id),
  FOREIGN KEY (participant_id) REFERENCES participants(id),
  UNIQUE (receipt_id, participant_id)
);

CREATE TABLE IF NOT EXISTS receipt_items (
  id INTEGER PRIMARY KEY,
  public_id TEXT NOT NULL UNIQUE,
  receipt_id INTEGER NOT NULL,
  line_number INTEGER,
  item_name TEXT NOT NULL,
  quantity NUMERIC,
  unit_price NUMERIC,
  line_amount NUMERIC NOT NULL,
  currency TEXT NOT NULL,
  category TEXT,
  ocr_confidence REAL CHECK (ocr_confidence IS NULL OR (ocr_confidence >= 0 AND ocr_confidence <= 1)),
  notes TEXT,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  FOREIGN KEY (receipt_id) REFERENCES receipts(id)
);

CREATE TABLE IF NOT EXISTS receipt_item_allocations (
  id INTEGER PRIMARY KEY,
  public_id TEXT NOT NULL UNIQUE,
  receipt_item_id INTEGER NOT NULL,
  participant_id INTEGER NOT NULL,
  share_quantity NUMERIC,
  share_amount_before_service_charge NUMERIC NOT NULL,
  allocation_method TEXT NOT NULL
    CHECK (allocation_method IN ('equal_quantity', 'equal_amount', 'manual', 'percentage', 'payer_only', 'excluded')),
  allocation_ratio NUMERIC,
  notes TEXT,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  FOREIGN KEY (receipt_item_id) REFERENCES receipt_items(id),
  FOREIGN KEY (participant_id) REFERENCES participants(id),
  UNIQUE (receipt_item_id, participant_id)
);

CREATE TABLE IF NOT EXISTS receipt_adjustments (
  id INTEGER PRIMARY KEY,
  public_id TEXT NOT NULL UNIQUE,
  receipt_id INTEGER NOT NULL,
  adjustment_type TEXT NOT NULL
    CHECK (adjustment_type IN ('service_charge', 'gst', 'discount', 'voucher', 'cashback', 'promo', 'manual_adjustment')),
  description TEXT,
  amount NUMERIC NOT NULL,
  currency TEXT NOT NULL,
  direction TEXT NOT NULL
    CHECK (direction IN ('add', 'subtract', 'informational')),
  rate NUMERIC,
  allocation_method TEXT NOT NULL
    CHECK (allocation_method IN ('proportional_by_item_amount', 'equal_per_participant', 'manual', 'payer_only', 'excluded', 'proportional_by_net_amount')),
  allocation_basis TEXT,
  cap_amount NUMERIC,
  priority INTEGER NOT NULL DEFAULT 100,
  source TEXT,
  raw_input TEXT,
  confidence_score REAL CHECK (confidence_score IS NULL OR (confidence_score >= 0 AND confidence_score <= 1)),
  notes TEXT,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  FOREIGN KEY (receipt_id) REFERENCES receipts(id)
);

CREATE TABLE IF NOT EXISTS payment_records (
  id INTEGER PRIMARY KEY,
  public_id TEXT NOT NULL UNIQUE,
  receipt_id INTEGER,
  payment_datetime TEXT,
  payer_participant_id INTEGER,
  payer_account_id INTEGER,
  payment_method TEXT,
  card_network TEXT,
  external_order_id TEXT,
  bill_amount NUMERIC,
  discount_amount NUMERIC,
  paid_amount NUMERIC NOT NULL,
  currency TEXT NOT NULL,
  attachment_id INTEGER,
  attachment_path TEXT,
  source TEXT,
  raw_input TEXT,
  confidence_score REAL CHECK (confidence_score IS NULL OR (confidence_score >= 0 AND confidence_score <= 1)),
  notes TEXT,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  FOREIGN KEY (receipt_id) REFERENCES receipts(id),
  FOREIGN KEY (payer_participant_id) REFERENCES participants(id),
  FOREIGN KEY (payer_account_id) REFERENCES accounts(id),
  FOREIGN KEY (attachment_id) REFERENCES attachments(id)
);

CREATE TABLE IF NOT EXISTS calculation_runs (
  id INTEGER PRIMARY KEY,
  public_id TEXT NOT NULL UNIQUE,
  calculation_version TEXT NOT NULL,
  scope_type TEXT NOT NULL CHECK (scope_type IN ('receipt', 'receipt_group')),
  receipt_id INTEGER,
  receipt_group_id INTEGER,
  currency TEXT NOT NULL,
  input_hash TEXT,
  calculation_status TEXT NOT NULL DEFAULT 'completed'
    CHECK (calculation_status IN ('pending', 'completed', 'failed', 'superseded', 'voided')),
  calculated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  source TEXT,
  raw_input TEXT,
  notes TEXT,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  FOREIGN KEY (receipt_id) REFERENCES receipts(id),
  FOREIGN KEY (receipt_group_id) REFERENCES receipt_groups(id),
  CHECK (
    (scope_type = 'receipt' AND receipt_id IS NOT NULL AND receipt_group_id IS NULL)
    OR
    (scope_type = 'receipt_group' AND receipt_group_id IS NOT NULL AND receipt_id IS NULL)
  )
);

CREATE TABLE IF NOT EXISTS calculation_participant_shares (
  id INTEGER PRIMARY KEY,
  public_id TEXT NOT NULL UNIQUE,
  calculation_run_id INTEGER NOT NULL,
  receipt_id INTEGER,
  participant_id INTEGER NOT NULL,
  item_share_amount NUMERIC NOT NULL DEFAULT 0,
  gross_share_amount NUMERIC NOT NULL DEFAULT 0,
  service_charge_share_amount NUMERIC NOT NULL DEFAULT 0,
  tax_share_amount NUMERIC NOT NULL DEFAULT 0,
  discount_share_amount NUMERIC NOT NULL DEFAULT 0,
  other_adjustment_share_amount NUMERIC NOT NULL DEFAULT 0,
  rounding_adjustment_amount NUMERIC NOT NULL DEFAULT 0,
  final_share_amount NUMERIC NOT NULL,
  currency TEXT NOT NULL,
  calculation_status TEXT NOT NULL DEFAULT 'completed'
    CHECK (calculation_status IN ('pending', 'completed', 'failed', 'superseded', 'voided')),
  calculated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  source TEXT,
  raw_input TEXT,
  notes TEXT,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  FOREIGN KEY (calculation_run_id) REFERENCES calculation_runs(id),
  FOREIGN KEY (receipt_id) REFERENCES receipts(id),
  FOREIGN KEY (participant_id) REFERENCES participants(id),
  UNIQUE (calculation_run_id, receipt_id, participant_id)
);

CREATE TABLE IF NOT EXISTS calculation_adjustment_allocations (
  id INTEGER PRIMARY KEY,
  public_id TEXT NOT NULL UNIQUE,
  calculation_run_id INTEGER NOT NULL,
  receipt_adjustment_id INTEGER NOT NULL,
  participant_id INTEGER NOT NULL,
  allocated_amount NUMERIC NOT NULL,
  rounding_adjustment_amount NUMERIC NOT NULL DEFAULT 0,
  currency TEXT NOT NULL,
  notes TEXT,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  FOREIGN KEY (calculation_run_id) REFERENCES calculation_runs(id),
  FOREIGN KEY (receipt_adjustment_id) REFERENCES receipt_adjustments(id),
  FOREIGN KEY (participant_id) REFERENCES participants(id),
  UNIQUE (calculation_run_id, receipt_adjustment_id, participant_id)
);

CREATE TABLE IF NOT EXISTS settlement_obligations (
  id INTEGER PRIMARY KEY,
  public_id TEXT NOT NULL UNIQUE,
  debtor_id INTEGER NOT NULL,
  creditor_id INTEGER NOT NULL,
  amount NUMERIC NOT NULL CHECK (amount >= 0),
  currency TEXT NOT NULL,
  source_calculation_run_id INTEGER NOT NULL,
  settlement_status TEXT NOT NULL DEFAULT 'open'
    CHECK (settlement_status IN ('open', 'partially_settled', 'settled', 'waived', 'voided')),
  paid_at TEXT,
  settlement_method TEXT,
  source TEXT,
  raw_input TEXT,
  notes TEXT,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  FOREIGN KEY (debtor_id) REFERENCES participants(id),
  FOREIGN KEY (creditor_id) REFERENCES participants(id),
  FOREIGN KEY (source_calculation_run_id) REFERENCES calculation_runs(id),
  CHECK (debtor_id <> creditor_id)
);

CREATE INDEX IF NOT EXISTS idx_receipt_groups_status ON receipt_groups(status);
CREATE INDEX IF NOT EXISTS idx_receipts_transaction_id ON receipts(transaction_id);
CREATE INDEX IF NOT EXISTS idx_receipts_merchant_datetime ON receipts(merchant, receipt_datetime);
CREATE INDEX IF NOT EXISTS idx_receipts_payer ON receipts(payer_participant_id);
CREATE INDEX IF NOT EXISTS idx_receipts_source_channel ON receipts(source_channel);
CREATE INDEX IF NOT EXISTS idx_receipts_attachment_id ON receipts(attachment_id);

CREATE INDEX IF NOT EXISTS idx_receipt_group_receipts_group ON receipt_group_receipts(receipt_group_id);
CREATE INDEX IF NOT EXISTS idx_receipt_group_receipts_receipt ON receipt_group_receipts(receipt_id);

CREATE INDEX IF NOT EXISTS idx_receipt_participants_receipt ON receipt_participants(receipt_id);
CREATE INDEX IF NOT EXISTS idx_receipt_participants_participant ON receipt_participants(participant_id);

CREATE INDEX IF NOT EXISTS idx_receipt_items_receipt ON receipt_items(receipt_id);
CREATE INDEX IF NOT EXISTS idx_receipt_items_category ON receipt_items(category);

CREATE INDEX IF NOT EXISTS idx_receipt_item_allocations_item ON receipt_item_allocations(receipt_item_id);
CREATE INDEX IF NOT EXISTS idx_receipt_item_allocations_participant ON receipt_item_allocations(participant_id);

CREATE INDEX IF NOT EXISTS idx_receipt_adjustments_receipt ON receipt_adjustments(receipt_id);
CREATE INDEX IF NOT EXISTS idx_receipt_adjustments_type ON receipt_adjustments(adjustment_type);
CREATE INDEX IF NOT EXISTS idx_receipt_adjustments_allocation_method ON receipt_adjustments(allocation_method);

CREATE INDEX IF NOT EXISTS idx_payment_records_receipt ON payment_records(receipt_id);
CREATE INDEX IF NOT EXISTS idx_payment_records_payer ON payment_records(payer_participant_id);
CREATE INDEX IF NOT EXISTS idx_payment_records_account ON payment_records(payer_account_id);
CREATE INDEX IF NOT EXISTS idx_payment_records_external_order ON payment_records(external_order_id);

CREATE INDEX IF NOT EXISTS idx_calculation_runs_receipt ON calculation_runs(receipt_id);
CREATE INDEX IF NOT EXISTS idx_calculation_runs_group ON calculation_runs(receipt_group_id);
CREATE INDEX IF NOT EXISTS idx_calculation_runs_status ON calculation_runs(calculation_status);
CREATE INDEX IF NOT EXISTS idx_calculation_runs_calculated_at ON calculation_runs(calculated_at);

CREATE INDEX IF NOT EXISTS idx_calculation_shares_run ON calculation_participant_shares(calculation_run_id);
CREATE INDEX IF NOT EXISTS idx_calculation_shares_receipt ON calculation_participant_shares(receipt_id);
CREATE INDEX IF NOT EXISTS idx_calculation_shares_participant ON calculation_participant_shares(participant_id);

CREATE INDEX IF NOT EXISTS idx_calculation_adjustments_run ON calculation_adjustment_allocations(calculation_run_id);
CREATE INDEX IF NOT EXISTS idx_calculation_adjustments_adjustment ON calculation_adjustment_allocations(receipt_adjustment_id);
CREATE INDEX IF NOT EXISTS idx_calculation_adjustments_participant ON calculation_adjustment_allocations(participant_id);

CREATE INDEX IF NOT EXISTS idx_settlement_obligations_debtor ON settlement_obligations(debtor_id);
CREATE INDEX IF NOT EXISTS idx_settlement_obligations_creditor ON settlement_obligations(creditor_id);
CREATE INDEX IF NOT EXISTS idx_settlement_obligations_run ON settlement_obligations(source_calculation_run_id);
CREATE INDEX IF NOT EXISTS idx_settlement_obligations_status ON settlement_obligations(settlement_status);
