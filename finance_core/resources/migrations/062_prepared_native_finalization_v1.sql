-- Offline exact finalization preparation. Native facts remain append-only.
CREATE TABLE l2_finalization_intents (
 operation_id TEXT PRIMARY KEY CHECK(length(operation_id) BETWEEN 1 AND 256),
 review_id TEXT NOT NULL UNIQUE REFERENCES l2_reviews(review_id),
 action_evidence_id TEXT NOT NULL UNIQUE CHECK(length(action_evidence_id) BETWEEN 1 AND 256),
 intent_sha256 TEXT NOT NULL UNIQUE CHECK(length(intent_sha256)=64 AND intent_sha256 NOT GLOB '*[^0-9a-f]*'),
 body_json TEXT NOT NULL CHECK(json_valid(body_json)),
 created_at INTEGER NOT NULL CHECK(created_at>0)
) STRICT;
CREATE TABLE l2_finalization_materials (
 package_sha256 TEXT PRIMARY KEY CHECK(length(package_sha256)=64 AND package_sha256 NOT GLOB '*[^0-9a-f]*'),
 payload BLOB NOT NULL CHECK(length(payload)>0)
) STRICT;
CREATE TABLE l2_finalization_outcomes (
 operation_id TEXT PRIMARY KEY REFERENCES l2_finalization_intents(operation_id),
 intent_sha256 TEXT NOT NULL CHECK(length(intent_sha256)=64 AND intent_sha256 NOT GLOB '*[^0-9a-f]*'),
 outcome TEXT NOT NULL CHECK(outcome IN ('APPLIED_OFFLINE','CANCELLED_OFFLINE')),
 resolved_at INTEGER NOT NULL CHECK(resolved_at>0),
 receipt_json TEXT CHECK(receipt_json IS NULL OR json_valid(receipt_json)),
 CHECK((outcome='APPLIED_OFFLINE' AND receipt_json IS NOT NULL)
    OR (outcome='CANCELLED_OFFLINE' AND receipt_json IS NULL))
) STRICT;
CREATE TRIGGER l2_finalization_single_pending BEFORE INSERT ON l2_finalization_intents
WHEN EXISTS(SELECT 1 FROM l2_finalization_intents i WHERE NOT EXISTS(
 SELECT 1 FROM l2_finalization_outcomes o WHERE o.operation_id=i.operation_id))
BEGIN SELECT RAISE(ABORT,'another finalization remains pending'); END;
CREATE TRIGGER l2_finalization_outcome_binding BEFORE INSERT ON l2_finalization_outcomes
WHEN NOT EXISTS(SELECT 1 FROM l2_finalization_intents i
 WHERE i.operation_id=NEW.operation_id AND i.intent_sha256=NEW.intent_sha256)
BEGIN SELECT RAISE(ABORT,'finalization outcome identity mismatch'); END;
CREATE TRIGGER l2_finalization_intents_no_update BEFORE UPDATE ON l2_finalization_intents BEGIN SELECT RAISE(ABORT,'finalization journal is append only'); END;
CREATE TRIGGER l2_finalization_intents_no_delete BEFORE DELETE ON l2_finalization_intents BEGIN SELECT RAISE(ABORT,'finalization journal is append only'); END;
CREATE TRIGGER l2_finalization_materials_no_update BEFORE UPDATE ON l2_finalization_materials BEGIN SELECT RAISE(ABORT,'finalization journal is append only'); END;
CREATE TRIGGER l2_finalization_materials_no_delete BEFORE DELETE ON l2_finalization_materials BEGIN SELECT RAISE(ABORT,'finalization journal is append only'); END;
CREATE TRIGGER l2_finalization_outcomes_no_update BEFORE UPDATE ON l2_finalization_outcomes BEGIN SELECT RAISE(ABORT,'finalization journal is append only'); END;
CREATE TRIGGER l2_finalization_outcomes_no_delete BEFORE DELETE ON l2_finalization_outcomes BEGIN SELECT RAISE(ABORT,'finalization journal is append only'); END;
CREATE TRIGGER l2_finalization_intents_no_collision BEFORE INSERT ON l2_finalization_intents
WHEN EXISTS(SELECT 1 FROM l2_finalization_intents WHERE operation_id=NEW.operation_id
 OR review_id=NEW.review_id OR action_evidence_id=NEW.action_evidence_id OR intent_sha256=NEW.intent_sha256)
BEGIN SELECT RAISE(ABORT,'finalization intent identity collision'); END;
CREATE TRIGGER l2_finalization_materials_no_collision BEFORE INSERT ON l2_finalization_materials
WHEN EXISTS(SELECT 1 FROM l2_finalization_materials WHERE package_sha256=NEW.package_sha256)
BEGIN SELECT RAISE(ABORT,'finalization material identity collision'); END;
CREATE TRIGGER l2_finalization_outcomes_no_collision BEFORE INSERT ON l2_finalization_outcomes
WHEN EXISTS(SELECT 1 FROM l2_finalization_outcomes WHERE operation_id=NEW.operation_id)
BEGIN SELECT RAISE(ABORT,'finalization outcome identity collision'); END;

-- Reserved operations can only append their exact pending compiled confirm.
-- Ordinary SQL cannot repurpose a cancelled operation and poison its journal.
CREATE TRIGGER l2_events_finalization_reservation BEFORE INSERT ON l2_events
WHEN EXISTS(
 SELECT 1 FROM l2_finalization_intents i WHERE i.operation_id=NEW.operation_id AND (
  EXISTS(SELECT 1 FROM l2_finalization_outcomes o WHERE o.operation_id=i.operation_id)
  OR NEW.kind IS NOT 'confirm'
  OR NEW.seq IS NOT json_extract(i.body_json,'$.append.event.seq')
  OR NEW.operation_id IS NOT json_extract(i.body_json,'$.append.event.operation_id')
  OR NEW.kind IS NOT json_extract(i.body_json,'$.append.event.kind')
  OR NEW.entity_id IS NOT json_extract(i.body_json,'$.append.event.entity_id')
  OR NEW.payload_json IS NOT json_extract(i.body_json,'$.append.event.payload_json')
  OR NEW.previous_sha256 IS NOT json_extract(i.body_json,'$.append.event.previous_sha256')
  OR NEW.event_sha256 IS NOT json_extract(i.body_json,'$.append.event.event_sha256')
 ))
BEGIN SELECT RAISE(ABORT,'operation identity is permanently reserved for finalization'); END;
