PRAGMA foreign_keys = ON;

INSERT OR IGNORE INTO participants (public_id, display_name, aliases, is_self, notes) VALUES
  ('person_member_a', 'MemberA', '["MemberA"]', 0, 'Test Case 001 participant'),
  ('person_member_b', 'MemberB', '["MemberB"]', 0, 'Test Case 001 participant'),
  ('person_member_c', 'MemberC', '["MemberC"]', 0, 'Test Case 001 participant'),
  ('person_member_d', 'MemberD', '["MemberD"]', 0, 'Test Case 001 participant'),
  ('person_member_e', 'MemberE', '["MemberE"]', 0, 'Test Case 001 participant');

INSERT OR IGNORE INTO receipt_groups (
  public_id, group_name, group_type, currency, status, source, raw_input, notes
) VALUES (
  'rg_test_case_001',
  'Test Case 001 - Example Restaurant and Example Tea',
  'shared_expense',
  'SGD',
  'calculated',
  'manual_test_case',
  'Multiple receipts with one payer, item ownership, capped discount, proportional discount, and final settlement.',
  'Target scenario for item-level receipt splitting and discount allocation.'
);

INSERT OR IGNORE INTO receipts (
  public_id, merchant, receipt_datetime, gross_amount, subtotal_amount,
  service_charge_amount, discount_amount, net_paid_amount, currency,
  payer_participant_id, source_channel, raw_input, attachment_path,
  payment_record_attachment_id, ocr_confidence, status, notes
) VALUES (
  'r_test_case_001_example_restaurant',
  'Example Restaurant',
  '2026-05-28 12:00:00',
  73.26,
  66.60,
  6.66,
  26.33,
  46.93,
  'SGD',
  (SELECT id FROM participants WHERE public_id = 'person_owner'),
  'manual_test_case',
  'Example Restaurant gross SGD 73.26, discount SGD 26.33, net paid SGD 46.93, paid by Owner.',
  '/data/evidence/receipts/test_case_001/example_restaurant_bill.jpg',
  NULL,
  NULL,
  'confirmed',
  'Service charge allocated proportional to item amount; capped promo allocated equally.'
);

INSERT OR IGNORE INTO receipts (
  public_id, merchant, receipt_datetime, gross_amount, subtotal_amount,
  discount_amount, net_paid_amount, currency, payer_participant_id,
  source_channel, raw_input, attachment_path, ocr_confidence, status, notes
) VALUES (
  'r_test_case_001_example_tea',
  'Example Tea',
  '2026-05-28 12:30:00',
  23.80,
  23.80,
  0.61,
  23.19,
  'SGD',
  (SELECT id FROM participants WHERE public_id = 'person_owner'),
  'manual_test_case',
  'Example Tea subtotal SGD 23.80, discount SGD 0.61, net paid SGD 23.19, paid by Owner.',
  '/data/evidence/receipts/test_case_001/example_tea_receipt.jpg',
  NULL,
  'confirmed',
  'Discount allocated proportionally by item amount.'
);

INSERT OR IGNORE INTO receipt_group_receipts (public_id, receipt_group_id, receipt_id, sequence_number) VALUES
  (
    'rgr_test_case_001_example_restaurant',
    (SELECT id FROM receipt_groups WHERE public_id = 'rg_test_case_001'),
    (SELECT id FROM receipts WHERE public_id = 'r_test_case_001_example_restaurant'),
    1
  ),
  (
    'rgr_test_case_001_example_tea',
    (SELECT id FROM receipt_groups WHERE public_id = 'rg_test_case_001'),
    (SELECT id FROM receipts WHERE public_id = 'r_test_case_001_example_tea'),
    2
  );

INSERT OR IGNORE INTO receipt_participants (public_id, receipt_id, participant_id, role, is_included) VALUES
  ('rp_example_restaurant_owner', (SELECT id FROM receipts WHERE public_id = 'r_test_case_001_example_restaurant'), (SELECT id FROM participants WHERE public_id = 'person_owner'), 'payer', 1),
  ('rp_example_restaurant_member_a', (SELECT id FROM receipts WHERE public_id = 'r_test_case_001_example_restaurant'), (SELECT id FROM participants WHERE public_id = 'person_member_a'), 'participant', 1),
  ('rp_example_restaurant_member_b', (SELECT id FROM receipts WHERE public_id = 'r_test_case_001_example_restaurant'), (SELECT id FROM participants WHERE public_id = 'person_member_b'), 'participant', 1),
  ('rp_example_restaurant_member_c', (SELECT id FROM receipts WHERE public_id = 'r_test_case_001_example_restaurant'), (SELECT id FROM participants WHERE public_id = 'person_member_c'), 'participant', 1),
  ('rp_example_restaurant_member_d', (SELECT id FROM receipts WHERE public_id = 'r_test_case_001_example_restaurant'), (SELECT id FROM participants WHERE public_id = 'person_member_d'), 'participant', 1),
  ('rp_example_restaurant_member_e', (SELECT id FROM receipts WHERE public_id = 'r_test_case_001_example_restaurant'), (SELECT id FROM participants WHERE public_id = 'person_member_e'), 'participant', 1),
  ('rp_example_tea_owner', (SELECT id FROM receipts WHERE public_id = 'r_test_case_001_example_tea'), (SELECT id FROM participants WHERE public_id = 'person_owner'), 'payer', 1),
  ('rp_example_tea_member_a', (SELECT id FROM receipts WHERE public_id = 'r_test_case_001_example_tea'), (SELECT id FROM participants WHERE public_id = 'person_member_a'), 'participant', 1),
  ('rp_example_tea_member_b', (SELECT id FROM receipts WHERE public_id = 'r_test_case_001_example_tea'), (SELECT id FROM participants WHERE public_id = 'person_member_b'), 'participant', 1);

INSERT OR IGNORE INTO receipt_items (
  public_id, receipt_id, line_number, item_name, quantity, unit_price,
  line_amount, currency, category, notes
) VALUES
  (
    'ri_example_restaurant_chicken_pot',
    (SELECT id FROM receipts WHERE public_id = 'r_test_case_001_example_restaurant'),
    1, 'Chicken Pot', 6, 10.90, 65.40, 'SGD', 'Food',
    'Six portions, one per participant.'
  ),
  (
    'ri_example_restaurant_water_chestnut',
    (SELECT id FROM receipts WHERE public_id = 'r_test_case_001_example_restaurant'),
    2, 'Water Chestnut', 1, 1.20, 1.20, 'SGD', 'Drink',
    'Allocated to MemberC only.'
  ),
  (
    'ri_example_tea_grapefruit_honey_slush',
    (SELECT id FROM receipts WHERE public_id = 'r_test_case_001_example_tea'),
    1, 'Grapefruit Honey Slush (Green Tea)', 1, 7.50, 7.50, 'SGD', 'Drink',
    'Allocated to MemberA.'
  ),
  (
    'ri_example_tea_grape_yakult_green_tea',
    (SELECT id FROM receipts WHERE public_id = 'r_test_case_001_example_tea'),
    2, 'Grape Yakult Green Tea', 2, 5.10, 10.20, 'SGD', 'Drink',
    'Allocated to MemberB.'
  ),
  (
    'ri_example_tea_chrysanthemum_tea',
    (SELECT id FROM receipts WHERE public_id = 'r_test_case_001_example_tea'),
    3, 'Chrysanthemum Tea', 1, 6.10, 6.10, 'SGD', 'Drink',
    'Allocated to Owner.'
  );

INSERT OR IGNORE INTO receipt_item_allocations (
  public_id, receipt_item_id, participant_id, share_quantity,
  share_amount_before_service_charge, allocation_method, allocation_ratio, notes
) VALUES
  ('ria_chicken_pot_owner', (SELECT id FROM receipt_items WHERE public_id = 'ri_example_restaurant_chicken_pot'), (SELECT id FROM participants WHERE public_id = 'person_owner'), 1, 10.90, 'equal_quantity', 1.0 / 6.0, 'One Chicken Pot portion.'),
  ('ria_chicken_pot_member_a', (SELECT id FROM receipt_items WHERE public_id = 'ri_example_restaurant_chicken_pot'), (SELECT id FROM participants WHERE public_id = 'person_member_a'), 1, 10.90, 'equal_quantity', 1.0 / 6.0, 'One Chicken Pot portion.'),
  ('ria_chicken_pot_member_b', (SELECT id FROM receipt_items WHERE public_id = 'ri_example_restaurant_chicken_pot'), (SELECT id FROM participants WHERE public_id = 'person_member_b'), 1, 10.90, 'equal_quantity', 1.0 / 6.0, 'One Chicken Pot portion.'),
  ('ria_chicken_pot_member_c', (SELECT id FROM receipt_items WHERE public_id = 'ri_example_restaurant_chicken_pot'), (SELECT id FROM participants WHERE public_id = 'person_member_c'), 1, 10.90, 'equal_quantity', 1.0 / 6.0, 'One Chicken Pot portion.'),
  ('ria_chicken_pot_member_d', (SELECT id FROM receipt_items WHERE public_id = 'ri_example_restaurant_chicken_pot'), (SELECT id FROM participants WHERE public_id = 'person_member_d'), 1, 10.90, 'equal_quantity', 1.0 / 6.0, 'One Chicken Pot portion.'),
  ('ria_chicken_pot_member_e', (SELECT id FROM receipt_items WHERE public_id = 'ri_example_restaurant_chicken_pot'), (SELECT id FROM participants WHERE public_id = 'person_member_e'), 1, 10.90, 'equal_quantity', 1.0 / 6.0, 'One Chicken Pot portion.'),
  ('ria_water_chestnut_member_c', (SELECT id FROM receipt_items WHERE public_id = 'ri_example_restaurant_water_chestnut'), (SELECT id FROM participants WHERE public_id = 'person_member_c'), 1, 1.20, 'manual', 1.0, 'MemberC only.'),
  ('ria_example_tea_grapefruit_member_a', (SELECT id FROM receipt_items WHERE public_id = 'ri_example_tea_grapefruit_honey_slush'), (SELECT id FROM participants WHERE public_id = 'person_member_a'), 1, 7.50, 'manual', 1.0, 'MemberA drink.'),
  ('ria_example_tea_grape_member_b', (SELECT id FROM receipt_items WHERE public_id = 'ri_example_tea_grape_yakult_green_tea'), (SELECT id FROM participants WHERE public_id = 'person_member_b'), 2, 10.20, 'manual', 1.0, 'MemberB drinks.'),
  ('ria_example_tea_chrysanthemum_owner', (SELECT id FROM receipt_items WHERE public_id = 'ri_example_tea_chrysanthemum_tea'), (SELECT id FROM participants WHERE public_id = 'person_owner'), 1, 6.10, 'manual', 1.0, 'Owner drink.');

INSERT OR IGNORE INTO receipt_adjustments (
  public_id, receipt_id, adjustment_type, description, amount, currency,
  direction, rate, allocation_method, allocation_basis, cap_amount, priority,
  source, raw_input, notes
) VALUES
  (
    'ra_example_restaurant_service_charge',
    (SELECT id FROM receipts WHERE public_id = 'r_test_case_001_example_restaurant'),
    'service_charge', '10% service charge', 6.66, 'SGD',
    'add', 0.10, 'proportional_by_item_amount', 'pre_service_item_amount', NULL, 10,
    'manual_test_case', '10% service charge.',
    'Python allocates this based on each participant pre-service-charge item amount.'
  ),
  (
    'ra_example_restaurant_capped_promo',
    (SELECT id FROM receipts WHERE public_id = 'r_test_case_001_example_restaurant'),
    'discount', 'Capped promotion discount', 26.33, 'SGD',
    'subtract', NULL, 'equal_per_participant', 'included_receipt_participants', 26.33, 20,
    'manual_test_case', 'SGD 26.33 capped promotion discount.',
    'Equal allocation prevents MemberC receiving a larger discount for ordering Water Chestnut.'
  ),
  (
    'ra_example_tea_discount',
    (SELECT id FROM receipts WHERE public_id = 'r_test_case_001_example_tea'),
    'discount', 'Receipt discount', 0.61, 'SGD',
    'subtract', NULL, 'proportional_by_item_amount', 'pre_discount_item_amount', NULL, 20,
    'manual_test_case', 'SGD 0.61 discount.',
    'Python allocates this proportionally by each participant item amount.'
  );

INSERT OR IGNORE INTO payment_records (
  public_id, receipt_id, payment_datetime, payer_participant_id, payer_account_id,
  payment_method, card_network, external_order_id, bill_amount, discount_amount,
  paid_amount, currency, attachment_path, source, raw_input, notes
) VALUES
  (
    'pay_example_restaurant_grab_test_case_001',
    (SELECT id FROM receipts WHERE public_id = 'r_test_case_001_example_restaurant'),
    '2026-05-28 12:05:00',
    (SELECT id FROM participants WHERE public_id = 'person_owner'),
    NULL,
    'card',
    'Mastercard',
    'GRAB-TEST-CASE-001',
    73.26,
    26.33,
    46.93,
    'SGD',
    '/data/evidence/payments/test_case_001/grab_payment_example_restaurant.jpg',
    'grab_payment_screenshot',
    'Grab payment record: bill SGD 73.26, deals and offers -SGD 26.33, final paid SGD 46.93, Mastercard.',
    'Links receipt paper bill to actual payment evidence.'
  ),
  (
    'pay_example_tea_test_case_001',
    (SELECT id FROM receipts WHERE public_id = 'r_test_case_001_example_tea'),
    '2026-05-28 12:35:00',
    (SELECT id FROM participants WHERE public_id = 'person_owner'),
    NULL,
    'card',
    NULL,
    'EXAMPLE-TEA-TEST-CASE-001',
    23.80,
    0.61,
    23.19,
    'SGD',
    '/data/evidence/payments/test_case_001/example_tea_payment.jpg',
    'manual_test_case',
    'Example Tea payment record: subtotal SGD 23.80, discount SGD 0.61, final paid SGD 23.19.',
    NULL
  );

INSERT OR IGNORE INTO calculation_runs (
  public_id, calculation_version, scope_type, receipt_group_id, currency,
  calculation_status, source, raw_input, notes
) VALUES (
  'calc_test_case_001_v1',
  'receipt_split_engine_v1',
  'receipt_group',
  (SELECT id FROM receipt_groups WHERE public_id = 'rg_test_case_001'),
  'SGD',
  'completed',
  'python_calculation_engine',
  'Test Case 001 expected calculation output.',
  'Python calculated item allocation, service charge allocation, discount allocation, rounding, and final obligations.'
);

INSERT OR IGNORE INTO calculation_participant_shares (
  public_id, calculation_run_id, receipt_id, participant_id, item_share_amount,
  gross_share_amount, service_charge_share_amount, discount_share_amount,
  rounding_adjustment_amount, final_share_amount, currency, source, notes
) VALUES
  ('cps_example_restaurant_owner', (SELECT id FROM calculation_runs WHERE public_id = 'calc_test_case_001_v1'), (SELECT id FROM receipts WHERE public_id = 'r_test_case_001_example_restaurant'), (SELECT id FROM participants WHERE public_id = 'person_owner'), 10.90, 11.99, 1.09, 4.39, 0.01, 7.61, 'SGD', 'python_calculation_engine', 'Owner absorbs +0.01 so final shares sum to net paid amount.'),
  ('cps_example_restaurant_member_a', (SELECT id FROM calculation_runs WHERE public_id = 'calc_test_case_001_v1'), (SELECT id FROM receipts WHERE public_id = 'r_test_case_001_example_restaurant'), (SELECT id FROM participants WHERE public_id = 'person_member_a'), 10.90, 11.99, 1.09, 4.39, 0.00, 7.60, 'SGD', 'python_calculation_engine', NULL),
  ('cps_example_restaurant_member_b', (SELECT id FROM calculation_runs WHERE public_id = 'calc_test_case_001_v1'), (SELECT id FROM receipts WHERE public_id = 'r_test_case_001_example_restaurant'), (SELECT id FROM participants WHERE public_id = 'person_member_b'), 10.90, 11.99, 1.09, 4.39, 0.00, 7.60, 'SGD', 'python_calculation_engine', NULL),
  ('cps_example_restaurant_member_c', (SELECT id FROM calculation_runs WHERE public_id = 'calc_test_case_001_v1'), (SELECT id FROM receipts WHERE public_id = 'r_test_case_001_example_restaurant'), (SELECT id FROM participants WHERE public_id = 'person_member_c'), 12.10, 13.31, 1.21, 4.39, 0.00, 8.92, 'SGD', 'python_calculation_engine', NULL),
  ('cps_example_restaurant_member_d', (SELECT id FROM calculation_runs WHERE public_id = 'calc_test_case_001_v1'), (SELECT id FROM receipts WHERE public_id = 'r_test_case_001_example_restaurant'), (SELECT id FROM participants WHERE public_id = 'person_member_d'), 10.90, 11.99, 1.09, 4.39, 0.00, 7.60, 'SGD', 'python_calculation_engine', NULL),
  ('cps_example_restaurant_member_e', (SELECT id FROM calculation_runs WHERE public_id = 'calc_test_case_001_v1'), (SELECT id FROM receipts WHERE public_id = 'r_test_case_001_example_restaurant'), (SELECT id FROM participants WHERE public_id = 'person_member_e'), 10.90, 11.99, 1.09, 4.39, 0.00, 7.60, 'SGD', 'python_calculation_engine', NULL),
  ('cps_example_tea_owner', (SELECT id FROM calculation_runs WHERE public_id = 'calc_test_case_001_v1'), (SELECT id FROM receipts WHERE public_id = 'r_test_case_001_example_tea'), (SELECT id FROM participants WHERE public_id = 'person_owner'), 6.10, 6.10, 0.00, 0.16, 0.00, 5.94, 'SGD', 'python_calculation_engine', NULL),
  ('cps_example_tea_member_a', (SELECT id FROM calculation_runs WHERE public_id = 'calc_test_case_001_v1'), (SELECT id FROM receipts WHERE public_id = 'r_test_case_001_example_tea'), (SELECT id FROM participants WHERE public_id = 'person_member_a'), 7.50, 7.50, 0.00, 0.19, 0.00, 7.31, 'SGD', 'python_calculation_engine', NULL),
  ('cps_example_tea_member_b', (SELECT id FROM calculation_runs WHERE public_id = 'calc_test_case_001_v1'), (SELECT id FROM receipts WHERE public_id = 'r_test_case_001_example_tea'), (SELECT id FROM participants WHERE public_id = 'person_member_b'), 10.20, 10.20, 0.00, 0.26, 0.00, 9.94, 'SGD', 'python_calculation_engine', NULL);

INSERT OR IGNORE INTO calculation_adjustment_allocations (
  public_id, calculation_run_id, receipt_adjustment_id, participant_id,
  allocated_amount, rounding_adjustment_amount, currency, notes
) VALUES
  ('caa_chicken_discount_owner', (SELECT id FROM calculation_runs WHERE public_id = 'calc_test_case_001_v1'), (SELECT id FROM receipt_adjustments WHERE public_id = 'ra_example_restaurant_capped_promo'), (SELECT id FROM participants WHERE public_id = 'person_owner'), 4.39, 0.01, 'SGD', 'Final share rounding assigned to Owner.'),
  ('caa_chicken_discount_member_a', (SELECT id FROM calculation_runs WHERE public_id = 'calc_test_case_001_v1'), (SELECT id FROM receipt_adjustments WHERE public_id = 'ra_example_restaurant_capped_promo'), (SELECT id FROM participants WHERE public_id = 'person_member_a'), 4.39, 0.00, 'SGD', NULL),
  ('caa_chicken_discount_member_b', (SELECT id FROM calculation_runs WHERE public_id = 'calc_test_case_001_v1'), (SELECT id FROM receipt_adjustments WHERE public_id = 'ra_example_restaurant_capped_promo'), (SELECT id FROM participants WHERE public_id = 'person_member_b'), 4.39, 0.00, 'SGD', NULL),
  ('caa_chicken_discount_member_c', (SELECT id FROM calculation_runs WHERE public_id = 'calc_test_case_001_v1'), (SELECT id FROM receipt_adjustments WHERE public_id = 'ra_example_restaurant_capped_promo'), (SELECT id FROM participants WHERE public_id = 'person_member_c'), 4.39, 0.00, 'SGD', NULL),
  ('caa_chicken_discount_member_d', (SELECT id FROM calculation_runs WHERE public_id = 'calc_test_case_001_v1'), (SELECT id FROM receipt_adjustments WHERE public_id = 'ra_example_restaurant_capped_promo'), (SELECT id FROM participants WHERE public_id = 'person_member_d'), 4.39, 0.00, 'SGD', NULL),
  ('caa_chicken_discount_member_e', (SELECT id FROM calculation_runs WHERE public_id = 'calc_test_case_001_v1'), (SELECT id FROM receipt_adjustments WHERE public_id = 'ra_example_restaurant_capped_promo'), (SELECT id FROM participants WHERE public_id = 'person_member_e'), 4.39, 0.00, 'SGD', NULL);

INSERT OR IGNORE INTO settlement_obligations (
  public_id, debtor_id, creditor_id, amount, currency,
  source_calculation_run_id, settlement_status, source, notes
) VALUES
  ('so_test_case_001_member_a_owes_owner', (SELECT id FROM participants WHERE public_id = 'person_member_a'), (SELECT id FROM participants WHERE public_id = 'person_owner'), 14.91, 'SGD', (SELECT id FROM calculation_runs WHERE public_id = 'calc_test_case_001_v1'), 'open', 'python_calculation_engine', 'MemberA owes Owner for Example Restaurant and Example Tea.'),
  ('so_test_case_001_member_b_owes_owner', (SELECT id FROM participants WHERE public_id = 'person_member_b'), (SELECT id FROM participants WHERE public_id = 'person_owner'), 17.54, 'SGD', (SELECT id FROM calculation_runs WHERE public_id = 'calc_test_case_001_v1'), 'open', 'python_calculation_engine', 'MemberB owes Owner for Example Restaurant and Example Tea.'),
  ('so_test_case_001_member_c_owes_owner', (SELECT id FROM participants WHERE public_id = 'person_member_c'), (SELECT id FROM participants WHERE public_id = 'person_owner'), 8.92, 'SGD', (SELECT id FROM calculation_runs WHERE public_id = 'calc_test_case_001_v1'), 'open', 'python_calculation_engine', 'MemberC owes Owner for Example Restaurant.'),
  ('so_test_case_001_member_d_owes_owner', (SELECT id FROM participants WHERE public_id = 'person_member_d'), (SELECT id FROM participants WHERE public_id = 'person_owner'), 7.60, 'SGD', (SELECT id FROM calculation_runs WHERE public_id = 'calc_test_case_001_v1'), 'open', 'python_calculation_engine', 'MemberD owes Owner for Example Restaurant.'),
  ('so_test_case_001_member_e_owes_owner', (SELECT id FROM participants WHERE public_id = 'person_member_e'), (SELECT id FROM participants WHERE public_id = 'person_owner'), 7.60, 'SGD', (SELECT id FROM calculation_runs WHERE public_id = 'calc_test_case_001_v1'), 'open', 'python_calculation_engine', 'MemberE owes Owner for Example Restaurant.');
