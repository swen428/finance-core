-- Synthetic Test Case 001 balances owed to the example owner.
PRAGMA foreign_keys = ON;

SELECT
  debtor.display_name AS debtor,
  creditor.display_name AS creditor,
  printf('%.2f', so.amount) AS amount,
  so.currency,
  so.settlement_status
FROM settlement_obligations so
JOIN participants debtor ON debtor.id = so.debtor_id
JOIN participants creditor ON creditor.id = so.creditor_id
JOIN calculation_runs cr ON cr.id = so.source_calculation_run_id
WHERE cr.public_id = 'calc_test_case_001_v1'
  AND creditor.public_id = 'person_owner'
ORDER BY debtor.display_name;
