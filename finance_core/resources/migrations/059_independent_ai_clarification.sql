-- Additive immutable clarification frontier; migration 058 remains unchanged.
CREATE TABLE independent_ai_clarification_requests (
 request_id TEXT PRIMARY KEY,
 attempt_id TEXT NOT NULL REFERENCES independent_ai_attempts(attempt_id),
 result_id TEXT NOT NULL REFERENCES independent_ai_results(result_id),
 generation INTEGER NOT NULL CHECK(typeof(generation)='integer' AND generation>=0),
 previous_request_id TEXT REFERENCES independent_ai_clarification_requests(request_id),
 intake_public_id TEXT NOT NULL REFERENCES raw_intake_records(public_id),
 parent_parser_output_id INTEGER NOT NULL REFERENCES parser_outputs(id),
 parent_version INTEGER NOT NULL CHECK(typeof(parent_version)='integer' AND parent_version>=0),
 parent_content_hash TEXT NOT NULL CHECK(length(parent_content_hash)=64 AND parent_content_hash NOT GLOB '*[^0-9a-f]*'),
 material_json TEXT NOT NULL CHECK(json_valid(material_json) AND json_type(material_json)='object'),
 material_hash TEXT NOT NULL UNIQUE CHECK(length(material_hash)=64 AND material_hash NOT GLOB '*[^0-9a-f]*'),
 created_at INTEGER NOT NULL CHECK(typeof(created_at)='integer' AND created_at>0),
 expires_at INTEGER NOT NULL CHECK(typeof(expires_at)='integer' AND expires_at>created_at),
 UNIQUE(attempt_id,generation), UNIQUE(previous_request_id)
);
CREATE TABLE independent_ai_followup_links (
 request_id TEXT PRIMARY KEY REFERENCES independent_ai_clarification_requests(request_id),
 predecessor_attempt_id TEXT NOT NULL UNIQUE REFERENCES independent_ai_attempts(attempt_id),
 successor_attempt_id TEXT NOT NULL UNIQUE REFERENCES independent_ai_attempts(attempt_id),
 source_event_key TEXT NOT NULL CHECK(length(source_event_key)=64 AND source_event_key NOT GLOB '*[^0-9a-f]*'),
 reply_evidence_id TEXT NOT NULL,
 reply_evidence_digest TEXT NOT NULL CHECK(length(reply_evidence_digest)=64 AND reply_evidence_digest NOT GLOB '*[^0-9a-f]*'),
 material_json TEXT NOT NULL CHECK(json_valid(material_json) AND json_type(material_json)='object'),
 material_hash TEXT NOT NULL UNIQUE CHECK(length(material_hash)=64 AND material_hash NOT GLOB '*[^0-9a-f]*'),
 accepted_at INTEGER NOT NULL CHECK(typeof(accepted_at)='integer' AND accepted_at>0),
 UNIQUE(source_event_key,reply_evidence_id), CHECK(predecessor_attempt_id!=successor_attempt_id)
);
CREATE TRIGGER independent_ai_clarification_requests_no_update BEFORE UPDATE ON independent_ai_clarification_requests BEGIN SELECT RAISE(ABORT,'immutable clarification evidence'); END;
CREATE TRIGGER independent_ai_clarification_requests_no_delete BEFORE DELETE ON independent_ai_clarification_requests BEGIN SELECT RAISE(ABORT,'immutable clarification evidence'); END;
CREATE TRIGGER independent_ai_clarification_requests_no_collision BEFORE INSERT ON independent_ai_clarification_requests WHEN EXISTS(SELECT 1 FROM independent_ai_clarification_requests WHERE request_id=NEW.request_id OR material_hash=NEW.material_hash OR (attempt_id=NEW.attempt_id AND generation=NEW.generation) OR previous_request_id=NEW.previous_request_id) BEGIN SELECT RAISE(ABORT,'clarification collision'); END;
CREATE TRIGGER independent_ai_followup_links_no_update BEFORE UPDATE ON independent_ai_followup_links BEGIN SELECT RAISE(ABORT,'immutable followup evidence'); END;
CREATE TRIGGER independent_ai_followup_links_no_delete BEFORE DELETE ON independent_ai_followup_links BEGIN SELECT RAISE(ABORT,'immutable followup evidence'); END;
CREATE TRIGGER independent_ai_followup_links_no_collision BEFORE INSERT ON independent_ai_followup_links WHEN EXISTS(SELECT 1 FROM independent_ai_followup_links WHERE request_id=NEW.request_id OR predecessor_attempt_id=NEW.predecessor_attempt_id OR successor_attempt_id=NEW.successor_attempt_id OR material_hash=NEW.material_hash OR (source_event_key=NEW.source_event_key AND reply_evidence_id=NEW.reply_evidence_id)) BEGIN SELECT RAISE(ABORT,'followup collision'); END;
