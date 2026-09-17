PRAGMA foreign_keys = ON;

-- Caller/adapter digests remain immutable external evidence. Only algorithms
-- with pinned reconstruction logic may populate the authoritative fingerprint.
ALTER TABLE statement_transactions
  ADD COLUMN external_row_fingerprint TEXT;

ALTER TABLE statement_transactions
  ADD COLUMN external_row_fingerprint_version TEXT;

CREATE TRIGGER trg_authoritative_statement_fingerprint_version_insert
BEFORE INSERT ON statement_transactions
WHEN NEW.row_fingerprint_version IS NOT NULL
  AND NEW.row_fingerprint_version NOT IN (
    'statement-row-fingerprint-v1',
    'pdf-row-fingerprint-v2'
  )
BEGIN
  SELECT RAISE(ABORT, 'unsupported authoritative statement fingerprint version');
END;

CREATE TRIGGER trg_external_statement_fingerprint_insert
BEFORE INSERT ON statement_transactions
WHEN CASE
  WHEN NEW.external_row_fingerprint IS NULL
    AND NEW.external_row_fingerprint_version IS NULL
  THEN 0
  WHEN NEW.external_row_fingerprint IS NULL
    OR length(NEW.external_row_fingerprint) != 64
    OR NEW.external_row_fingerprint GLOB '*[^0-9a-f]*'
    OR NEW.external_row_fingerprint_version IS NULL
    OR trim(NEW.external_row_fingerprint_version) = ''
  THEN 1
  ELSE 0
END
BEGIN
  SELECT RAISE(ABORT, 'invalid external statement fingerprint evidence');
END;

CREATE TRIGGER trg_external_statement_fingerprint_no_update
BEFORE UPDATE ON statement_transactions
WHEN OLD.external_row_fingerprint IS NOT NULL
  OR OLD.external_row_fingerprint_version IS NOT NULL
  OR NEW.external_row_fingerprint IS NOT OLD.external_row_fingerprint
  OR NEW.external_row_fingerprint_version IS NOT OLD.external_row_fingerprint_version
BEGIN
  SELECT RAISE(ABORT, 'external statement fingerprint evidence is append-only');
END;

CREATE TRIGGER trg_external_statement_fingerprint_no_delete
BEFORE DELETE ON statement_transactions
WHEN OLD.external_row_fingerprint IS NOT NULL
  OR OLD.external_row_fingerprint_version IS NOT NULL
BEGIN
  SELECT RAISE(ABORT, 'external statement fingerprint evidence is append-only');
END;

-- Authoritative reconciliation decisions are append-only. Partial proof rows
-- and proof material that disagrees with authoritative relational columns are
-- rejected before they can become durable.
CREATE TRIGGER trg_authoritative_reconciliation_decision_insert
BEFORE INSERT ON reconciliation_match_results
WHEN (
  NEW.decision_contract_version IS NOT NULL
  OR NEW.matcher_version IS NOT NULL
  OR NEW.compatibility_version IS NOT NULL
  OR NEW.merchant_normalization_version IS NOT NULL
  OR NEW.candidate_set_fingerprint IS NOT NULL
  OR NEW.decision_hash IS NOT NULL
  OR NEW.decision_material_json IS NOT NULL
) AND CASE
  WHEN NEW.decision_contract_version IS NULL
    OR trim(NEW.decision_contract_version) = ''
    OR NEW.matcher_version IS NULL
    OR trim(NEW.matcher_version) = ''
    OR NEW.compatibility_version IS NULL
    OR trim(NEW.compatibility_version) = ''
    OR NEW.merchant_normalization_version IS NULL
    OR trim(NEW.merchant_normalization_version) = ''
    OR NEW.candidate_set_fingerprint IS NULL
    OR length(NEW.candidate_set_fingerprint) != 64
    OR NEW.candidate_set_fingerprint GLOB '*[^0-9a-f]*'
    OR NEW.decision_hash IS NULL
    OR length(NEW.decision_hash) != 64
    OR NEW.decision_hash GLOB '*[^0-9a-f]*'
    OR NEW.decision_material_json IS NULL
    OR json_valid(NEW.decision_material_json) != 1
    OR json_valid(NEW.reason_codes_json) != 1
    OR json_valid(NEW.evidence_json) != 1
  THEN 1
  ELSE (
    json_type(NEW.decision_material_json, '$.contract_version') IS NOT 'text'
    OR json_extract(NEW.decision_material_json, '$.contract_version')
         IS NOT 'finance-canonical-json-v1'
    OR json_type(NEW.decision_material_json, '$.value') IS NOT 'object'
    OR json_type(NEW.reason_codes_json) IS NOT 'array'
    OR json_type(NEW.evidence_json) IS NOT 'object'
    OR json_type(
         NEW.decision_material_json,
         '$.value.decision_contract_version'
       ) IS NOT 'text'
    OR json_extract(
         NEW.decision_material_json,
         '$.value.decision_contract_version'
       ) IS NOT NEW.decision_contract_version
    OR json_type(NEW.decision_material_json, '$.value.matcher_version') IS NOT 'text'
    OR json_extract(NEW.decision_material_json, '$.value.matcher_version')
         IS NOT NEW.matcher_version
    OR json_type(
         NEW.decision_material_json,
         '$.value.compatibility_version'
       ) IS NOT 'text'
    OR json_extract(
         NEW.decision_material_json,
         '$.value.compatibility_version'
       ) IS NOT NEW.compatibility_version
    OR json_type(
         NEW.decision_material_json,
         '$.value.merchant_normalization_version'
       ) IS NOT 'text'
    OR json_extract(
         NEW.decision_material_json,
         '$.value.merchant_normalization_version'
       ) IS NOT NEW.merchant_normalization_version
    OR json_type(
         NEW.decision_material_json,
         '$.value.candidate_set_fingerprint'
       ) IS NOT 'text'
    OR json_extract(
         NEW.decision_material_json,
         '$.value.candidate_set_fingerprint'
       ) IS NOT NEW.candidate_set_fingerprint
    OR json_type(NEW.decision_material_json, '$.value.candidates') IS NOT 'array'
    OR json_type(NEW.decision_material_json, '$.value.statement') IS NOT 'object'
    OR json_type(NEW.decision_material_json, '$.value.thresholds') IS NOT 'object'
    OR json_type(NEW.decision_material_json, '$.value.final_decision') IS NOT 'object'
    OR json_type(
         NEW.decision_material_json,
         '$.value.final_decision.status'
       ) IS NOT 'text'
    OR json_extract(
         NEW.decision_material_json,
         '$.value.final_decision.status'
       ) IS NOT NEW.match_status
    OR json_type(
         NEW.decision_material_json,
         '$.value.final_decision.reason_codes'
       ) IS NOT 'array'
    OR json(json_extract(
         NEW.decision_material_json,
         '$.value.final_decision.reason_codes'
       )) != json(NEW.reason_codes_json)
    OR json_extract(
         NEW.decision_material_json,
         '$.value.final_decision.best_candidate_public_id'
       ) IS NOT NEW.internal_candidate_id
    OR json_extract(
         NEW.decision_material_json,
         '$.value.final_decision.authorization_public_id'
       ) IS NOT NEW.authorization_public_id
    OR NEW.needs_review IS NOT CASE WHEN NEW.match_status = 'matched' THEN 0 ELSE 1 END
    OR NOT EXISTS (
      SELECT 1
      FROM statement_transactions AS st
      JOIN statement_import_batches AS b ON b.id = st.batch_id
      WHERE st.id = NEW.statement_transaction_id
        AND json_extract(
              NEW.decision_material_json,
              '$.value.statement.public_id'
            ) IS st.public_id
        AND json_extract(
              NEW.decision_material_json,
              '$.value.statement.row_fingerprint'
            ) IS st.row_fingerprint
        AND json_extract(
              NEW.decision_material_json,
              '$.value.statement.source_content_hash'
            ) IS b.source_file_hash
        AND json_extract(
              NEW.decision_material_json,
              '$.value.statement.source_batch_id'
            ) IS CAST(st.batch_id AS TEXT)
        AND json_extract(
              NEW.decision_material_json,
              '$.value.statement.row_reference'
            ) IS st.statement_row_reference
        AND json_extract(
              NEW.decision_material_json,
              '$.value.statement.original_amount_text'
            ) IS st.raw_amount
        AND json_extract(
              NEW.decision_material_json,
              '$.value.statement.original_amount_type'
            ) IS st.raw_amount_type
        AND json_type(
              NEW.decision_material_json,
              '$.value.statement.normalized_amount'
            ) IS 'text'
        AND json_extract(
              NEW.decision_material_json,
              '$.value.statement.normalized_amount'
            ) IS trim(json_extract(
              NEW.decision_material_json,
              '$.value.statement.normalized_amount'
            ))
        AND json_extract(
              NEW.decision_material_json,
              '$.value.statement.normalized_amount'
            ) != ''
        AND json_extract(
              NEW.decision_material_json,
              '$.value.statement.normalized_amount'
            ) NOT GLOB '*[^0-9.]*'
        AND json_extract(
              NEW.decision_material_json,
              '$.value.statement.normalized_amount'
            ) NOT LIKE '%.%.%'
        AND json_extract(
              NEW.decision_material_json,
              '$.value.statement.normalized_amount'
            ) NOT LIKE '.%'
        AND json_extract(
              NEW.decision_material_json,
              '$.value.statement.normalized_amount'
            ) NOT LIKE '%.'
        AND NOT (
          length(json_extract(
            NEW.decision_material_json,
            '$.value.statement.normalized_amount'
          )) > 1
          AND substr(json_extract(
            NEW.decision_material_json,
            '$.value.statement.normalized_amount'
          ), 1, 1) = '0'
          AND substr(json_extract(
            NEW.decision_material_json,
            '$.value.statement.normalized_amount'
          ), 2, 1) != '.'
        )
        AND NOT (
          instr(json_extract(
            NEW.decision_material_json,
            '$.value.statement.normalized_amount'
          ), '.') > 0
          AND (
            length(json_extract(
              NEW.decision_material_json,
              '$.value.statement.normalized_amount'
            )) - instr(json_extract(
              NEW.decision_material_json,
              '$.value.statement.normalized_amount'
            ), '.') > 2
            OR substr(json_extract(
              NEW.decision_material_json,
              '$.value.statement.normalized_amount'
            ), -1, 1) = '0'
          )
        )
        AND CAST(json_extract(
              NEW.decision_material_json,
              '$.value.statement.normalized_amount'
            ) AS NUMERIC) = st.amount
        AND json_extract(
              NEW.decision_material_json,
              '$.value.statement.currency'
            ) IS st.currency
        AND json_extract(
              NEW.decision_material_json,
              '$.value.statement.direction'
            ) IS st.amount_direction
        AND json_extract(
              NEW.decision_material_json,
              '$.value.statement.transaction_date'
            ) IS st.transaction_date
        AND json_extract(
              NEW.decision_material_json,
              '$.value.statement.posted_date'
            ) IS st.posted_date
    )
  )
END
BEGIN
  SELECT RAISE(ABORT, 'invalid authoritative reconciliation decision proof');
END;

CREATE TRIGGER trg_authoritative_reconciliation_decision_no_update
BEFORE UPDATE ON reconciliation_match_results
WHEN OLD.decision_contract_version IS NOT NULL
  OR OLD.matcher_version IS NOT NULL
  OR OLD.compatibility_version IS NOT NULL
  OR OLD.merchant_normalization_version IS NOT NULL
  OR OLD.candidate_set_fingerprint IS NOT NULL
  OR OLD.decision_hash IS NOT NULL
  OR OLD.decision_material_json IS NOT NULL
  OR NEW.decision_contract_version IS NOT OLD.decision_contract_version
  OR NEW.matcher_version IS NOT OLD.matcher_version
  OR NEW.compatibility_version IS NOT OLD.compatibility_version
  OR NEW.merchant_normalization_version IS NOT OLD.merchant_normalization_version
  OR NEW.candidate_set_fingerprint IS NOT OLD.candidate_set_fingerprint
  OR NEW.decision_hash IS NOT OLD.decision_hash
  OR NEW.decision_material_json IS NOT OLD.decision_material_json
BEGIN
  SELECT RAISE(ABORT, 'authoritative reconciliation decision is append-only');
END;

CREATE TRIGGER trg_authoritative_reconciliation_decision_no_delete
BEFORE DELETE ON reconciliation_match_results
WHEN OLD.decision_contract_version IS NOT NULL
  OR OLD.matcher_version IS NOT NULL
  OR OLD.compatibility_version IS NOT NULL
  OR OLD.merchant_normalization_version IS NOT NULL
  OR OLD.candidate_set_fingerprint IS NOT NULL
  OR OLD.decision_hash IS NOT NULL
  OR OLD.decision_material_json IS NOT NULL
BEGIN
  SELECT RAISE(ABORT, 'authoritative reconciliation decision is append-only');
END;

-- One authoritative source-content identity owns one accepted import command.
-- Exact command replays reuse the existing batch in the service; changed
-- metadata cannot create a second batch for the same source bytes.
CREATE TRIGGER trg_authoritative_statement_source_owner_insert
BEFORE INSERT ON statement_import_batches
WHEN NEW.import_contract_version IS NOT NULL
  AND NEW.source_file_hash IS NOT NULL
  AND EXISTS (
    SELECT 1
    FROM statement_import_batches AS existing
    WHERE existing.import_contract_version IS NOT NULL
      AND existing.source_file_hash = NEW.source_file_hash
  )
BEGIN
  SELECT RAISE(ABORT, 'authoritative statement source content already has an owner');
END;

CREATE TRIGGER trg_authoritative_statement_batch_no_update
BEFORE UPDATE ON statement_import_batches
WHEN OLD.import_contract_version IS NOT NULL
  OR OLD.import_command_hash IS NOT NULL
  OR OLD.row_set_fingerprint IS NOT NULL
  OR NEW.import_contract_version IS NOT OLD.import_contract_version
  OR NEW.import_command_hash IS NOT OLD.import_command_hash
  OR NEW.row_set_fingerprint IS NOT OLD.row_set_fingerprint
BEGIN
  SELECT RAISE(ABORT, 'authoritative statement batch is append-only');
END;

CREATE TRIGGER trg_authoritative_statement_batch_no_delete
BEFORE DELETE ON statement_import_batches
WHEN OLD.import_contract_version IS NOT NULL
  OR OLD.import_command_hash IS NOT NULL
  OR OLD.row_set_fingerprint IS NOT NULL
BEGIN
  SELECT RAISE(ABORT, 'authoritative statement batch is append-only');
END;

CREATE TRIGGER trg_statement_import_source_evidence_no_update
BEFORE UPDATE ON statement_import_source_evidence
BEGIN
  SELECT RAISE(ABORT, 'statement source evidence is append-only');
END;

CREATE TRIGGER trg_statement_import_source_evidence_no_delete
BEFORE DELETE ON statement_import_source_evidence
BEGIN
  SELECT RAISE(ABORT, 'statement source evidence is append-only');
END;

CREATE TRIGGER trg_authoritative_statement_row_no_update
BEFORE UPDATE ON statement_transactions
WHEN OLD.row_fingerprint_version IS NOT NULL
  OR NEW.row_fingerprint_version IS NOT OLD.row_fingerprint_version
BEGIN
  SELECT RAISE(ABORT, 'authoritative statement row is append-only');
END;

CREATE TRIGGER trg_authoritative_statement_row_no_delete
BEFORE DELETE ON statement_transactions
WHEN OLD.row_fingerprint_version IS NOT NULL
BEGIN
  SELECT RAISE(ABORT, 'authoritative statement row is append-only');
END;

-- Replace migration 028's NULL-sensitive PDF checks with typed, fail-closed
-- validation. CASE prevents json_extract/json_type from evaluating malformed
-- JSON before the stable trigger error is raised.
DROP TRIGGER trg_pdf_statement_evidence_insert;
DROP TRIGGER trg_pdf_statement_evidence_update;

CREATE TRIGGER trg_pdf_statement_evidence_insert
BEFORE INSERT ON statement_transactions
WHEN NEW.row_fingerprint_version = 'pdf-row-fingerprint-v2' AND CASE
  WHEN NEW.raw_row_payload_json IS NULL
    OR json_valid(NEW.raw_row_payload_json) != 1
  THEN 1
  ELSE (
    json_type(NEW.raw_row_payload_json, '$.evidence_contract_version') IS NOT 'text'
    OR json_extract(NEW.raw_row_payload_json, '$.evidence_contract_version')
         IS NOT 'pdf-row-evidence-v2'
    OR json_type(NEW.raw_row_payload_json, '$.row_fingerprint') IS NOT 'text'
    OR length(json_extract(NEW.raw_row_payload_json, '$.row_fingerprint')) != 64
    OR json_extract(NEW.raw_row_payload_json, '$.row_fingerprint') GLOB '*[^0-9a-f]*'
    OR json_extract(NEW.raw_row_payload_json, '$.row_fingerprint') IS NOT NEW.row_fingerprint
    OR json_type(NEW.raw_row_payload_json, '$.row_fingerprint_version') IS NOT 'text'
    OR json_extract(NEW.raw_row_payload_json, '$.row_fingerprint_version')
         IS NOT NEW.row_fingerprint_version
    OR json_type(NEW.raw_row_payload_json, '$.review_status') IS NOT 'text'
    OR json_extract(NEW.raw_row_payload_json, '$.review_status') IS NOT 'authoritative'
    OR json_type(NEW.raw_row_payload_json, '$.source_content_hash') IS NOT 'text'
    OR length(json_extract(NEW.raw_row_payload_json, '$.source_content_hash')) != 64
    OR json_extract(NEW.raw_row_payload_json, '$.source_content_hash') GLOB '*[^0-9a-f]*'
    OR json_extract(NEW.raw_row_payload_json, '$.source_content_hash') IS NOT (
      SELECT source_file_hash FROM statement_import_batches WHERE id = NEW.batch_id
    )
    OR json_type(NEW.raw_row_payload_json, '$.source_page_number') IS NOT 'integer'
    OR json_extract(NEW.raw_row_payload_json, '$.source_page_number') < 1
    OR (
      json_type(NEW.raw_row_payload_json, '$.source_row_number') IS NOT NULL
      AND (
        json_type(NEW.raw_row_payload_json, '$.source_row_number') IS NOT 'integer'
        OR json_extract(NEW.raw_row_payload_json, '$.source_row_number') < 1
      )
    )
    OR json_type(NEW.raw_row_payload_json, '$.stable_row_locator') IS NOT 'text'
    OR trim(json_extract(NEW.raw_row_payload_json, '$.stable_row_locator')) = ''
    OR json_extract(NEW.raw_row_payload_json, '$.stable_row_locator')
         IS NOT trim(json_extract(NEW.raw_row_payload_json, '$.stable_row_locator'))
    OR json_type(NEW.raw_row_payload_json, '$.attachment_path') IS NOT 'text'
    OR trim(json_extract(NEW.raw_row_payload_json, '$.attachment_path')) = ''
    OR json_type(NEW.raw_row_payload_json, '$.source_text_excerpt') IS NOT 'text'
    OR trim(json_extract(NEW.raw_row_payload_json, '$.source_text_excerpt')) = ''
    OR json_type(NEW.raw_row_payload_json, '$.original_line_text') IS NOT 'text'
    OR trim(json_extract(NEW.raw_row_payload_json, '$.original_line_text')) = ''
    OR json_type(NEW.raw_row_payload_json, '$.parser_name') IS NOT 'text'
    OR trim(json_extract(NEW.raw_row_payload_json, '$.parser_name')) = ''
    OR json_type(NEW.raw_row_payload_json, '$.parser_version') IS NOT 'text'
    OR trim(json_extract(NEW.raw_row_payload_json, '$.parser_version')) = ''
    OR json_type(NEW.raw_row_payload_json, '$.template_name') IS NOT 'text'
    OR trim(json_extract(NEW.raw_row_payload_json, '$.template_name')) = ''
    OR json_type(NEW.raw_row_payload_json, '$.template_version') IS NOT 'text'
    OR trim(json_extract(NEW.raw_row_payload_json, '$.template_version')) = ''
    OR json_type(NEW.raw_row_payload_json, '$.extraction_version') IS NOT 'text'
    OR trim(json_extract(NEW.raw_row_payload_json, '$.extraction_version')) = ''
    OR json_type(NEW.raw_row_payload_json, '$.direction_source') IS NOT 'text'
    OR json_extract(NEW.raw_row_payload_json, '$.direction_source')
         NOT IN ('explicit_token', 'explicit_column')
    OR json_type(NEW.raw_row_payload_json, '$.direction_confidence') IS NOT 'text'
    OR json_extract(NEW.raw_row_payload_json, '$.direction_confidence') IS NOT 'high'
    OR json_type(NEW.raw_row_payload_json, '$.direction') IS NOT 'text'
    OR json_extract(NEW.raw_row_payload_json, '$.direction') IS NOT NEW.amount_direction
    OR NEW.amount_direction IS NULL
    OR NEW.amount_direction IN ('unknown', 'interest')
    OR json_type(NEW.raw_row_payload_json, '$.original_amount_token') IS NOT 'text'
    OR trim(json_extract(NEW.raw_row_payload_json, '$.original_amount_token')) = ''
    OR json_extract(NEW.raw_row_payload_json, '$.original_amount_token') IS NOT
         trim(json_extract(NEW.raw_row_payload_json, '$.original_amount_token'))
    OR json_extract(NEW.raw_row_payload_json, '$.original_amount_token') GLOB '+ *'
    OR json_extract(NEW.raw_row_payload_json, '$.original_amount_token') GLOB '- *'
    OR json_extract(NEW.raw_row_payload_json, '$.original_amount_token') GLOB '( *'
    OR json_extract(NEW.raw_row_payload_json, '$.original_amount_token') GLOB '* )'
    OR json_extract(NEW.raw_row_payload_json, '$.original_amount_token') IS NOT NEW.raw_amount
    OR json_type(NEW.raw_row_payload_json, '$.original_amount_sign') IS NOT 'text'
    OR json_extract(NEW.raw_row_payload_json, '$.original_amount_sign')
         NOT IN ('positive', 'negative')
    OR json_type(NEW.raw_row_payload_json, '$.amount_sign_convention') IS NOT 'text'
    OR json_extract(NEW.raw_row_payload_json, '$.amount_sign_convention')
         NOT IN ('unsigned_explicit', 'outflow_positive', 'outflow_negative')
    OR json_type(NEW.raw_row_payload_json, '$.normalized_amount') IS NOT 'text'
    OR trim(json_extract(NEW.raw_row_payload_json, '$.normalized_amount')) = ''
    OR json_extract(NEW.raw_row_payload_json, '$.normalized_amount') IS NOT
         trim(json_extract(NEW.raw_row_payload_json, '$.normalized_amount'))
    OR json_extract(NEW.raw_row_payload_json, '$.normalized_amount') GLOB '*[^0-9.]*'
    OR json_extract(NEW.raw_row_payload_json, '$.normalized_amount') LIKE '%.%.%'
    OR json_extract(NEW.raw_row_payload_json, '$.normalized_amount') LIKE '.%'
    OR json_extract(NEW.raw_row_payload_json, '$.normalized_amount') LIKE '%.'
    OR (
      length(json_extract(NEW.raw_row_payload_json, '$.normalized_amount')) > 1
      AND substr(json_extract(NEW.raw_row_payload_json, '$.normalized_amount'), 1, 1) = '0'
      AND substr(json_extract(NEW.raw_row_payload_json, '$.normalized_amount'), 2, 1) != '.'
    )
    OR (
      instr(json_extract(NEW.raw_row_payload_json, '$.normalized_amount'), '.') > 0
      AND (
        length(json_extract(NEW.raw_row_payload_json, '$.normalized_amount'))
          - instr(json_extract(NEW.raw_row_payload_json, '$.normalized_amount'), '.') > 2
        OR substr(json_extract(NEW.raw_row_payload_json, '$.normalized_amount'), -1, 1) = '0'
      )
    )
    OR CAST(json_extract(NEW.raw_row_payload_json, '$.normalized_amount') AS NUMERIC)
         != NEW.amount
    OR json_type(NEW.raw_row_payload_json, '$.currency_token') IS NOT 'text'
    OR trim(json_extract(NEW.raw_row_payload_json, '$.currency_token')) = ''
    OR json_type(NEW.raw_row_payload_json, '$.currency_source') IS NOT 'text'
    OR trim(json_extract(NEW.raw_row_payload_json, '$.currency_source')) = ''
    OR json_type(NEW.raw_row_payload_json, '$.currency_resolution') IS NOT 'object'
    OR json_type(
         NEW.raw_row_payload_json,
         '$.currency_resolution.resolved_currency'
       ) IS NOT 'text'
    OR json_extract(
         NEW.raw_row_payload_json,
         '$.currency_resolution.resolved_currency'
       ) IS NOT NEW.currency
    OR NEW.currency IS NOT upper(trim(NEW.currency))
    OR json_extract(
         NEW.raw_row_payload_json,
         '$.currency_resolution.currency_token'
       ) IS NOT upper(trim(json_extract(NEW.raw_row_payload_json, '$.currency_token')))
    OR CASE upper(trim(json_extract(NEW.raw_row_payload_json, '$.currency_token')))
      WHEN 'S$' THEN json_extract(
        NEW.raw_row_payload_json, '$.currency_resolution.currency_token_currency'
      ) IS NOT 'SGD'
      WHEN 'SGD' THEN json_extract(
        NEW.raw_row_payload_json, '$.currency_resolution.currency_token_currency'
      ) IS NOT 'SGD'
      WHEN 'RM' THEN json_extract(
        NEW.raw_row_payload_json, '$.currency_resolution.currency_token_currency'
      ) IS NOT 'MYR'
      WHEN 'MYR' THEN json_extract(
        NEW.raw_row_payload_json, '$.currency_resolution.currency_token_currency'
      ) IS NOT 'MYR'
      WHEN 'USD' THEN json_extract(
        NEW.raw_row_payload_json, '$.currency_resolution.currency_token_currency'
      ) IS NOT 'USD'
      WHEN '$' THEN json_type(
        NEW.raw_row_payload_json, '$.currency_resolution.currency_token_currency'
      ) IS NOT NULL
      ELSE (
        length(upper(trim(json_extract(NEW.raw_row_payload_json, '$.currency_token')))) != 3
        OR upper(trim(json_extract(NEW.raw_row_payload_json, '$.currency_token')))
             GLOB '*[^A-Z]*'
        OR json_extract(
             NEW.raw_row_payload_json,
             '$.currency_resolution.currency_token_currency'
           ) IS NOT upper(trim(json_extract(NEW.raw_row_payload_json, '$.currency_token')))
      )
    END
    OR CASE
      WHEN substr(upper(ltrim(json_extract(
        NEW.raw_row_payload_json, '$.original_amount_token'
      ), '+-(')), 1, 3) = 'SGD' THEN (
        json_extract(
          NEW.raw_row_payload_json, '$.currency_resolution.amount_token_prefix'
        ) IS NOT 'SGD'
        OR json_extract(
          NEW.raw_row_payload_json, '$.currency_resolution.amount_token_currency'
        ) IS NOT 'SGD'
      )
      WHEN substr(upper(ltrim(json_extract(
        NEW.raw_row_payload_json, '$.original_amount_token'
      ), '+-(')), 1, 3) = 'MYR' THEN (
        json_extract(
          NEW.raw_row_payload_json, '$.currency_resolution.amount_token_prefix'
        ) IS NOT 'MYR'
        OR json_extract(
          NEW.raw_row_payload_json, '$.currency_resolution.amount_token_currency'
        ) IS NOT 'MYR'
      )
      WHEN substr(upper(ltrim(json_extract(
        NEW.raw_row_payload_json, '$.original_amount_token'
      ), '+-(')), 1, 3) = 'USD' THEN (
        json_extract(
          NEW.raw_row_payload_json, '$.currency_resolution.amount_token_prefix'
        ) IS NOT 'USD'
        OR json_extract(
          NEW.raw_row_payload_json, '$.currency_resolution.amount_token_currency'
        ) IS NOT 'USD'
      )
      WHEN substr(upper(ltrim(json_extract(
        NEW.raw_row_payload_json, '$.original_amount_token'
      ), '+-(')), 1, 2) = 'S$' THEN (
        json_extract(
          NEW.raw_row_payload_json, '$.currency_resolution.amount_token_prefix'
        ) IS NOT 'S$'
        OR json_extract(
          NEW.raw_row_payload_json, '$.currency_resolution.amount_token_currency'
        ) IS NOT 'SGD'
      )
      WHEN substr(upper(ltrim(json_extract(
        NEW.raw_row_payload_json, '$.original_amount_token'
      ), '+-(')), 1, 2) = 'RM' THEN (
        json_extract(
          NEW.raw_row_payload_json, '$.currency_resolution.amount_token_prefix'
        ) IS NOT 'RM'
        OR json_extract(
          NEW.raw_row_payload_json, '$.currency_resolution.amount_token_currency'
        ) IS NOT 'MYR'
      )
      WHEN substr(upper(ltrim(json_extract(
        NEW.raw_row_payload_json, '$.original_amount_token'
      ), '+-(')), 1, 1) = '$' THEN (
        json_extract(
          NEW.raw_row_payload_json, '$.currency_resolution.amount_token_prefix'
        ) IS NOT '$'
        OR json_type(
          NEW.raw_row_payload_json, '$.currency_resolution.amount_token_currency'
        ) IS NOT NULL
      )
      ELSE (
        json_type(
          NEW.raw_row_payload_json, '$.currency_resolution.amount_token_prefix'
        ) IS NOT NULL
        OR json_type(
          NEW.raw_row_payload_json, '$.currency_resolution.amount_token_currency'
        ) IS NOT NULL
      )
    END
    OR (
      json_type(
        NEW.raw_row_payload_json, '$.currency_resolution.amount_token_currency'
      ) IS NOT NULL
      AND json_extract(
        NEW.raw_row_payload_json, '$.currency_resolution.amount_token_currency'
      ) IS NOT NEW.currency
    )
    OR (
      json_type(
        NEW.raw_row_payload_json, '$.currency_resolution.currency_token_currency'
      ) IS NOT NULL
      AND json_extract(
        NEW.raw_row_payload_json, '$.currency_resolution.currency_token_currency'
      ) IS NOT NEW.currency
    )
    OR (
      NEW.transaction_date IS NOT NULL
      AND (
        json_type(NEW.raw_row_payload_json, '$.transaction_date_token') IS NOT 'text'
        OR trim(json_extract(NEW.raw_row_payload_json, '$.transaction_date_token')) = ''
      )
    )
    OR (
      NEW.posted_date IS NOT NULL
      AND (
        json_type(NEW.raw_row_payload_json, '$.posted_date_token') IS NOT 'text'
        OR trim(json_extract(NEW.raw_row_payload_json, '$.posted_date_token')) = ''
      )
    )
    OR NEW.statement_row_reference IS NULL
    OR NEW.statement_row_reference = ''
    OR NEW.statement_row_reference IS NOT json_extract(
      NEW.raw_row_payload_json,
      '$.stable_row_locator'
    )
  )
END
BEGIN
  SELECT RAISE(ABORT, 'invalid authoritative PDF statement evidence');
END;

CREATE TRIGGER trg_pdf_statement_evidence_update
BEFORE UPDATE ON statement_transactions
WHEN NEW.row_fingerprint_version = 'pdf-row-fingerprint-v2' AND CASE
  WHEN NEW.raw_row_payload_json IS NULL
    OR json_valid(NEW.raw_row_payload_json) != 1
  THEN 1
  ELSE (
    json_type(NEW.raw_row_payload_json, '$.evidence_contract_version') IS NOT 'text'
    OR json_extract(NEW.raw_row_payload_json, '$.evidence_contract_version')
         IS NOT 'pdf-row-evidence-v2'
    OR json_type(NEW.raw_row_payload_json, '$.row_fingerprint') IS NOT 'text'
    OR length(json_extract(NEW.raw_row_payload_json, '$.row_fingerprint')) != 64
    OR json_extract(NEW.raw_row_payload_json, '$.row_fingerprint') GLOB '*[^0-9a-f]*'
    OR json_extract(NEW.raw_row_payload_json, '$.row_fingerprint') IS NOT NEW.row_fingerprint
    OR json_type(NEW.raw_row_payload_json, '$.row_fingerprint_version') IS NOT 'text'
    OR json_extract(NEW.raw_row_payload_json, '$.row_fingerprint_version')
         IS NOT NEW.row_fingerprint_version
    OR json_type(NEW.raw_row_payload_json, '$.review_status') IS NOT 'text'
    OR json_extract(NEW.raw_row_payload_json, '$.review_status') IS NOT 'authoritative'
    OR json_type(NEW.raw_row_payload_json, '$.source_content_hash') IS NOT 'text'
    OR length(json_extract(NEW.raw_row_payload_json, '$.source_content_hash')) != 64
    OR json_extract(NEW.raw_row_payload_json, '$.source_content_hash') GLOB '*[^0-9a-f]*'
    OR json_extract(NEW.raw_row_payload_json, '$.source_content_hash') IS NOT (
      SELECT source_file_hash FROM statement_import_batches WHERE id = NEW.batch_id
    )
    OR json_type(NEW.raw_row_payload_json, '$.source_page_number') IS NOT 'integer'
    OR json_extract(NEW.raw_row_payload_json, '$.source_page_number') < 1
    OR (
      json_type(NEW.raw_row_payload_json, '$.source_row_number') IS NOT NULL
      AND (
        json_type(NEW.raw_row_payload_json, '$.source_row_number') IS NOT 'integer'
        OR json_extract(NEW.raw_row_payload_json, '$.source_row_number') < 1
      )
    )
    OR json_type(NEW.raw_row_payload_json, '$.stable_row_locator') IS NOT 'text'
    OR trim(json_extract(NEW.raw_row_payload_json, '$.stable_row_locator')) = ''
    OR json_extract(NEW.raw_row_payload_json, '$.stable_row_locator')
         IS NOT trim(json_extract(NEW.raw_row_payload_json, '$.stable_row_locator'))
    OR json_type(NEW.raw_row_payload_json, '$.attachment_path') IS NOT 'text'
    OR trim(json_extract(NEW.raw_row_payload_json, '$.attachment_path')) = ''
    OR json_type(NEW.raw_row_payload_json, '$.source_text_excerpt') IS NOT 'text'
    OR trim(json_extract(NEW.raw_row_payload_json, '$.source_text_excerpt')) = ''
    OR json_type(NEW.raw_row_payload_json, '$.original_line_text') IS NOT 'text'
    OR trim(json_extract(NEW.raw_row_payload_json, '$.original_line_text')) = ''
    OR json_type(NEW.raw_row_payload_json, '$.parser_name') IS NOT 'text'
    OR trim(json_extract(NEW.raw_row_payload_json, '$.parser_name')) = ''
    OR json_type(NEW.raw_row_payload_json, '$.parser_version') IS NOT 'text'
    OR trim(json_extract(NEW.raw_row_payload_json, '$.parser_version')) = ''
    OR json_type(NEW.raw_row_payload_json, '$.template_name') IS NOT 'text'
    OR trim(json_extract(NEW.raw_row_payload_json, '$.template_name')) = ''
    OR json_type(NEW.raw_row_payload_json, '$.template_version') IS NOT 'text'
    OR trim(json_extract(NEW.raw_row_payload_json, '$.template_version')) = ''
    OR json_type(NEW.raw_row_payload_json, '$.extraction_version') IS NOT 'text'
    OR trim(json_extract(NEW.raw_row_payload_json, '$.extraction_version')) = ''
    OR json_type(NEW.raw_row_payload_json, '$.direction_source') IS NOT 'text'
    OR json_extract(NEW.raw_row_payload_json, '$.direction_source')
         NOT IN ('explicit_token', 'explicit_column')
    OR json_type(NEW.raw_row_payload_json, '$.direction_confidence') IS NOT 'text'
    OR json_extract(NEW.raw_row_payload_json, '$.direction_confidence') IS NOT 'high'
    OR json_type(NEW.raw_row_payload_json, '$.direction') IS NOT 'text'
    OR json_extract(NEW.raw_row_payload_json, '$.direction') IS NOT NEW.amount_direction
    OR NEW.amount_direction IS NULL
    OR NEW.amount_direction IN ('unknown', 'interest')
    OR json_type(NEW.raw_row_payload_json, '$.original_amount_token') IS NOT 'text'
    OR trim(json_extract(NEW.raw_row_payload_json, '$.original_amount_token')) = ''
    OR json_extract(NEW.raw_row_payload_json, '$.original_amount_token') IS NOT
         trim(json_extract(NEW.raw_row_payload_json, '$.original_amount_token'))
    OR json_extract(NEW.raw_row_payload_json, '$.original_amount_token') GLOB '+ *'
    OR json_extract(NEW.raw_row_payload_json, '$.original_amount_token') GLOB '- *'
    OR json_extract(NEW.raw_row_payload_json, '$.original_amount_token') GLOB '( *'
    OR json_extract(NEW.raw_row_payload_json, '$.original_amount_token') GLOB '* )'
    OR json_extract(NEW.raw_row_payload_json, '$.original_amount_token') IS NOT NEW.raw_amount
    OR json_type(NEW.raw_row_payload_json, '$.original_amount_sign') IS NOT 'text'
    OR json_extract(NEW.raw_row_payload_json, '$.original_amount_sign')
         NOT IN ('positive', 'negative')
    OR json_type(NEW.raw_row_payload_json, '$.amount_sign_convention') IS NOT 'text'
    OR json_extract(NEW.raw_row_payload_json, '$.amount_sign_convention')
         NOT IN ('unsigned_explicit', 'outflow_positive', 'outflow_negative')
    OR json_type(NEW.raw_row_payload_json, '$.normalized_amount') IS NOT 'text'
    OR trim(json_extract(NEW.raw_row_payload_json, '$.normalized_amount')) = ''
    OR json_extract(NEW.raw_row_payload_json, '$.normalized_amount') IS NOT
         trim(json_extract(NEW.raw_row_payload_json, '$.normalized_amount'))
    OR json_extract(NEW.raw_row_payload_json, '$.normalized_amount') GLOB '*[^0-9.]*'
    OR json_extract(NEW.raw_row_payload_json, '$.normalized_amount') LIKE '%.%.%'
    OR json_extract(NEW.raw_row_payload_json, '$.normalized_amount') LIKE '.%'
    OR json_extract(NEW.raw_row_payload_json, '$.normalized_amount') LIKE '%.'
    OR (
      length(json_extract(NEW.raw_row_payload_json, '$.normalized_amount')) > 1
      AND substr(json_extract(NEW.raw_row_payload_json, '$.normalized_amount'), 1, 1) = '0'
      AND substr(json_extract(NEW.raw_row_payload_json, '$.normalized_amount'), 2, 1) != '.'
    )
    OR (
      instr(json_extract(NEW.raw_row_payload_json, '$.normalized_amount'), '.') > 0
      AND (
        length(json_extract(NEW.raw_row_payload_json, '$.normalized_amount'))
          - instr(json_extract(NEW.raw_row_payload_json, '$.normalized_amount'), '.') > 2
        OR substr(json_extract(NEW.raw_row_payload_json, '$.normalized_amount'), -1, 1) = '0'
      )
    )
    OR CAST(json_extract(NEW.raw_row_payload_json, '$.normalized_amount') AS NUMERIC)
         != NEW.amount
    OR json_type(NEW.raw_row_payload_json, '$.currency_token') IS NOT 'text'
    OR trim(json_extract(NEW.raw_row_payload_json, '$.currency_token')) = ''
    OR json_type(NEW.raw_row_payload_json, '$.currency_source') IS NOT 'text'
    OR trim(json_extract(NEW.raw_row_payload_json, '$.currency_source')) = ''
    OR json_type(NEW.raw_row_payload_json, '$.currency_resolution') IS NOT 'object'
    OR json_type(
         NEW.raw_row_payload_json,
         '$.currency_resolution.resolved_currency'
       ) IS NOT 'text'
    OR json_extract(
         NEW.raw_row_payload_json,
         '$.currency_resolution.resolved_currency'
       ) IS NOT NEW.currency
    OR NEW.currency IS NOT upper(trim(NEW.currency))
    OR json_extract(
         NEW.raw_row_payload_json,
         '$.currency_resolution.currency_token'
       ) IS NOT upper(trim(json_extract(NEW.raw_row_payload_json, '$.currency_token')))
    OR CASE upper(trim(json_extract(NEW.raw_row_payload_json, '$.currency_token')))
      WHEN 'S$' THEN json_extract(
        NEW.raw_row_payload_json, '$.currency_resolution.currency_token_currency'
      ) IS NOT 'SGD'
      WHEN 'SGD' THEN json_extract(
        NEW.raw_row_payload_json, '$.currency_resolution.currency_token_currency'
      ) IS NOT 'SGD'
      WHEN 'RM' THEN json_extract(
        NEW.raw_row_payload_json, '$.currency_resolution.currency_token_currency'
      ) IS NOT 'MYR'
      WHEN 'MYR' THEN json_extract(
        NEW.raw_row_payload_json, '$.currency_resolution.currency_token_currency'
      ) IS NOT 'MYR'
      WHEN 'USD' THEN json_extract(
        NEW.raw_row_payload_json, '$.currency_resolution.currency_token_currency'
      ) IS NOT 'USD'
      WHEN '$' THEN json_type(
        NEW.raw_row_payload_json, '$.currency_resolution.currency_token_currency'
      ) IS NOT NULL
      ELSE (
        length(upper(trim(json_extract(NEW.raw_row_payload_json, '$.currency_token')))) != 3
        OR upper(trim(json_extract(NEW.raw_row_payload_json, '$.currency_token')))
             GLOB '*[^A-Z]*'
        OR json_extract(
             NEW.raw_row_payload_json,
             '$.currency_resolution.currency_token_currency'
           ) IS NOT upper(trim(json_extract(NEW.raw_row_payload_json, '$.currency_token')))
      )
    END
    OR CASE
      WHEN substr(upper(ltrim(json_extract(
        NEW.raw_row_payload_json, '$.original_amount_token'
      ), '+-(')), 1, 3) = 'SGD' THEN (
        json_extract(
          NEW.raw_row_payload_json, '$.currency_resolution.amount_token_prefix'
        ) IS NOT 'SGD'
        OR json_extract(
          NEW.raw_row_payload_json, '$.currency_resolution.amount_token_currency'
        ) IS NOT 'SGD'
      )
      WHEN substr(upper(ltrim(json_extract(
        NEW.raw_row_payload_json, '$.original_amount_token'
      ), '+-(')), 1, 3) = 'MYR' THEN (
        json_extract(
          NEW.raw_row_payload_json, '$.currency_resolution.amount_token_prefix'
        ) IS NOT 'MYR'
        OR json_extract(
          NEW.raw_row_payload_json, '$.currency_resolution.amount_token_currency'
        ) IS NOT 'MYR'
      )
      WHEN substr(upper(ltrim(json_extract(
        NEW.raw_row_payload_json, '$.original_amount_token'
      ), '+-(')), 1, 3) = 'USD' THEN (
        json_extract(
          NEW.raw_row_payload_json, '$.currency_resolution.amount_token_prefix'
        ) IS NOT 'USD'
        OR json_extract(
          NEW.raw_row_payload_json, '$.currency_resolution.amount_token_currency'
        ) IS NOT 'USD'
      )
      WHEN substr(upper(ltrim(json_extract(
        NEW.raw_row_payload_json, '$.original_amount_token'
      ), '+-(')), 1, 2) = 'S$' THEN (
        json_extract(
          NEW.raw_row_payload_json, '$.currency_resolution.amount_token_prefix'
        ) IS NOT 'S$'
        OR json_extract(
          NEW.raw_row_payload_json, '$.currency_resolution.amount_token_currency'
        ) IS NOT 'SGD'
      )
      WHEN substr(upper(ltrim(json_extract(
        NEW.raw_row_payload_json, '$.original_amount_token'
      ), '+-(')), 1, 2) = 'RM' THEN (
        json_extract(
          NEW.raw_row_payload_json, '$.currency_resolution.amount_token_prefix'
        ) IS NOT 'RM'
        OR json_extract(
          NEW.raw_row_payload_json, '$.currency_resolution.amount_token_currency'
        ) IS NOT 'MYR'
      )
      WHEN substr(upper(ltrim(json_extract(
        NEW.raw_row_payload_json, '$.original_amount_token'
      ), '+-(')), 1, 1) = '$' THEN (
        json_extract(
          NEW.raw_row_payload_json, '$.currency_resolution.amount_token_prefix'
        ) IS NOT '$'
        OR json_type(
          NEW.raw_row_payload_json, '$.currency_resolution.amount_token_currency'
        ) IS NOT NULL
      )
      ELSE (
        json_type(
          NEW.raw_row_payload_json, '$.currency_resolution.amount_token_prefix'
        ) IS NOT NULL
        OR json_type(
          NEW.raw_row_payload_json, '$.currency_resolution.amount_token_currency'
        ) IS NOT NULL
      )
    END
    OR (
      json_type(
        NEW.raw_row_payload_json, '$.currency_resolution.amount_token_currency'
      ) IS NOT NULL
      AND json_extract(
        NEW.raw_row_payload_json, '$.currency_resolution.amount_token_currency'
      ) IS NOT NEW.currency
    )
    OR (
      json_type(
        NEW.raw_row_payload_json, '$.currency_resolution.currency_token_currency'
      ) IS NOT NULL
      AND json_extract(
        NEW.raw_row_payload_json, '$.currency_resolution.currency_token_currency'
      ) IS NOT NEW.currency
    )
    OR (
      NEW.transaction_date IS NOT NULL
      AND (
        json_type(NEW.raw_row_payload_json, '$.transaction_date_token') IS NOT 'text'
        OR trim(json_extract(NEW.raw_row_payload_json, '$.transaction_date_token')) = ''
      )
    )
    OR (
      NEW.posted_date IS NOT NULL
      AND (
        json_type(NEW.raw_row_payload_json, '$.posted_date_token') IS NOT 'text'
        OR trim(json_extract(NEW.raw_row_payload_json, '$.posted_date_token')) = ''
      )
    )
    OR NEW.statement_row_reference IS NULL
    OR NEW.statement_row_reference = ''
    OR NEW.statement_row_reference IS NOT json_extract(
      NEW.raw_row_payload_json,
      '$.stable_row_locator'
    )
  )
END
BEGIN
  SELECT RAISE(ABORT, 'invalid authoritative PDF statement evidence');
END;
