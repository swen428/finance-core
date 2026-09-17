PRAGMA foreign_keys = ON;

INSERT OR IGNORE INTO participants (
  public_id,
  display_name,
  aliases,
  is_self,
  notes
) VALUES
  ('person_owner', 'Owner', '["Owner","me","我"]', 1, 'Primary participant'),
  ('person_b', 'B', '["B"]', 0, 'Test participant B'),
  ('person_c', 'C', '["C"]', 0, 'Test participant C');

INSERT OR IGNORE INTO accounts (
  public_id,
  account_name,
  account_type,
  institution,
  default_currency,
  country,
  notes
) VALUES
  ('acct_cash_wallet_sgd', 'Cash Wallet SGD', 'cash', NULL, 'SGD', 'SG', 'Physical cash wallet'),
  ('acct_uob_lady_card_sgd', 'UOB Lady Card SGD', 'credit_card', 'UOB', 'SGD', 'SG', 'UOB Lady Card'),
  ('acct_moomoo_sgd', 'Moomoo SGD', 'investment', 'Moomoo', 'SGD', 'SG', 'Moomoo investment account');

INSERT OR IGNORE INTO transactions (
  public_id,
  intent,
  intent_type,
  source_channel,
  transaction_date,
  status,
  review_status,
  account_id,
  amount,
  currency,
  merchant,
  category,
  notes,
  raw_input
) VALUES (
  'txn_expense_kopi_20260524',
  'expense',
  'Manual',
  'telegram',
  '2026-05-24',
  'active',
  'reviewed',
  (SELECT id FROM accounts WHERE public_id = 'acct_cash_wallet_sgd'),
  2.20,
  'SGD',
  'Kopitiam',
  'Food',
  'Seed normal expense',
  'kopi 2.20'
);

INSERT OR IGNORE INTO transactions (
  public_id,
  intent,
  intent_type,
  source_channel,
  transaction_date,
  status,
  review_status,
  account_id,
  paid_by_participant_id,
  total_amount,
  currency,
  merchant,
  category,
  split_type,
  notes,
  raw_input
) VALUES (
  'txn_shared_example_shop_20260524',
  'shared_expense',
  'Manual',
  'telegram',
  '2026-05-24',
  'active',
  'reviewed',
  (SELECT id FROM accounts WHERE public_id = 'acct_uob_lady_card_sgd'),
  (SELECT id FROM participants WHERE public_id = 'person_owner'),
  9.00,
  'SGD',
  'Example Shop',
  'Books',
  'equal',
  'Example Shop split equally between Owner and B',
  'Example Shop 9 split with B'
);

INSERT OR IGNORE INTO shared_expense_obligations (
  public_id,
  shared_expense_transaction_id,
  participant_id,
  owed_to_participant_id,
  share_amount,
  currency,
  split_type,
  split_ratio,
  is_excluded,
  status,
  notes
) VALUES
  (
    'obl_example_shop_owner_20260524',
    (SELECT id FROM transactions WHERE public_id = 'txn_shared_example_shop_20260524'),
    (SELECT id FROM participants WHERE public_id = 'person_owner'),
    (SELECT id FROM participants WHERE public_id = 'person_owner'),
    4.50,
    'SGD',
    'equal',
    0.5,
    0,
    'settled',
    'Owner paid the original expense and owns this share'
  ),
  (
    'obl_example_shop_b_20260524',
    (SELECT id FROM transactions WHERE public_id = 'txn_shared_example_shop_20260524'),
    (SELECT id FROM participants WHERE public_id = 'person_b'),
    (SELECT id FROM participants WHERE public_id = 'person_owner'),
    4.50,
    'SGD',
    'equal',
    0.5,
    0,
    'settled',
    'B owes Owner SGD 4.50'
  );

INSERT OR IGNORE INTO transactions (
  public_id,
  intent,
  intent_type,
  source_channel,
  transaction_date,
  status,
  review_status,
  account_id,
  from_participant_id,
  amount,
  currency,
  category,
  notes,
  raw_input
) VALUES (
  'txn_reimbursement_b_example_shop_20260524',
  'reimbursement_received',
  'Manual',
  'telegram',
  '2026-05-24',
  'active',
  'reviewed',
  (SELECT id FROM accounts WHERE public_id = 'acct_cash_wallet_sgd'),
  (SELECT id FROM participants WHERE public_id = 'person_b'),
  4.50,
  'SGD',
  'Reimbursement',
  'B repaid Owner for Example Shop shared expense',
  'B paid me 4.50 for Example Shop'
);

INSERT OR IGNORE INTO transaction_links (
  public_id,
  source_transaction_id,
  target_transaction_id,
  link_type,
  amount,
  currency,
  status,
  notes
) VALUES (
  'link_reimbursement_b_to_example_shop_20260524',
  (SELECT id FROM transactions WHERE public_id = 'txn_reimbursement_b_example_shop_20260524'),
  (SELECT id FROM transactions WHERE public_id = 'txn_shared_example_shop_20260524'),
  'reimbursement_for',
  4.50,
  'SGD',
  'active',
  'B reimbursement linked to the original shared expense'
);

-- Verification commands:
-- sqlite3 database/finance.db < finance_core/resources/migrations/001_create_core_schema.sql
-- sqlite3 database/finance.db < database/seed/001_seed_initial_data.sql
-- sqlite3 database/finance.db < database/queries/shared_expense_balance.sql
