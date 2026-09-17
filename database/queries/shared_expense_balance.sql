PRAGMA foreign_keys = ON;

.headers on
.mode column

WITH self_participant AS (
  SELECT id
  FROM participants
  WHERE is_self = 1
  ORDER BY id
  LIMIT 1
),
reimbursement_payments AS (
  SELECT
    tl.target_transaction_id AS shared_expense_transaction_id,
    CASE
      WHEN rt.intent = 'reimbursement_received' THEN rt.from_participant_id
      WHEN rt.intent = 'reimbursement_paid' THEN (SELECT id FROM self_participant)
      ELSE NULL
    END AS participant_id,
    SUM(COALESCE(tl.amount, rt.amount, 0)) AS amount_paid
  FROM transaction_links tl
  JOIN transactions rt
    ON rt.id = tl.source_transaction_id
  WHERE tl.link_type = 'reimbursement_for'
    AND tl.status = 'active'
    AND rt.status = 'active'
    AND rt.intent IN ('reimbursement_received', 'reimbursement_paid')
  GROUP BY
    tl.target_transaction_id,
    CASE
      WHEN rt.intent = 'reimbursement_received' THEN rt.from_participant_id
      WHEN rt.intent = 'reimbursement_paid' THEN (SELECT id FROM self_participant)
      ELSE NULL
    END
),
balances AS (
  SELECT
    se.public_id AS shared_expense_public_id,
    se.transaction_date AS date,
    se.merchant,
    payer.display_name AS paid_by,
    participant.display_name AS participant,
    obl.share_amount,
    CASE
      WHEN obl.participant_id = se.paid_by_participant_id THEN obl.share_amount
      ELSE COALESCE(rp.amount_paid, 0)
    END AS amount_paid,
    CASE
      WHEN obl.participant_id = se.paid_by_participant_id THEN 0
      ELSE MAX(obl.share_amount - COALESCE(rp.amount_paid, 0), 0)
    END AS outstanding_amount,
    obl.status AS recorded_status
  FROM shared_expense_obligations obl
  JOIN transactions se
    ON se.id = obl.shared_expense_transaction_id
  JOIN participants payer
    ON payer.id = se.paid_by_participant_id
  JOIN participants participant
    ON participant.id = obl.participant_id
  LEFT JOIN reimbursement_payments rp
    ON rp.shared_expense_transaction_id = obl.shared_expense_transaction_id
   AND rp.participant_id = obl.participant_id
  WHERE se.intent = 'shared_expense'
    AND se.status = 'active'
)
SELECT
  shared_expense_public_id,
  date,
  merchant,
  paid_by,
  participant,
  printf('%.2f', share_amount) AS share_amount,
  printf('%.2f', amount_paid) AS amount_paid,
  printf('%.2f', outstanding_amount) AS outstanding_amount,
  CASE
    WHEN outstanding_amount <= 0 THEN 'settled'
    WHEN amount_paid > 0 THEN 'partially_settled'
    ELSE recorded_status
  END AS status
FROM balances
ORDER BY date, shared_expense_public_id, participant;

-- Verification commands:
-- sqlite3 database/finance.db < finance_core/resources/migrations/001_create_core_schema.sql
-- sqlite3 database/finance.db < database/seed/001_seed_initial_data.sql
-- sqlite3 database/finance.db < database/queries/shared_expense_balance.sql
