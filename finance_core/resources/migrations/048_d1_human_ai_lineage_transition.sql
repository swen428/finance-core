-- Permit only a sealed D1 human-publication edge to advance a raw-intake
-- pointer away from an immutable AI fallback child. The AI child and its
-- evidence remain sealed; arbitrary reparse children remain refused.

DROP TRIGGER trg_ai_fallback_raw_intake_no_lineage_escape;

CREATE TRIGGER trg_ai_fallback_raw_intake_no_lineage_escape
    BEFORE UPDATE OF parser_output_id ON raw_intake_records
    WHEN OLD.parser_output_id IS NOT NEW.parser_output_id
      AND NOT EXISTS (
          SELECT 1
          FROM parser_human_draft_publications AS publication
          JOIN parser_human_draft_operations AS operation
            ON operation.id = publication.operation_id
           AND operation.draft_id = publication.draft_id
          JOIN parser_outputs AS child
            ON child.id = publication.parser_output_id
          WHERE publication.parser_output_id = NEW.parser_output_id
            AND child.parent_parser_output_id = OLD.parser_output_id
            AND operation.operation_type = 'accepted'
            AND operation.operation_outcome = 'accepted'
            AND operation.result_completeness = 'complete'
            AND operation.publication_parser_output_id = NEW.parser_output_id
      )
      AND (
          EXISTS (
              SELECT 1
              FROM ai_fallback_proposal_links
              WHERE parser_output_id = OLD.parser_output_id
          )
          OR EXISTS (
              SELECT 1
              FROM ai_fallback_attempts AS attempt
              WHERE attempt.parent_parser_output_id = OLD.parser_output_id
                AND NOT EXISTS (
                    SELECT 1
                    FROM ai_fallback_proposal_links AS link
                    JOIN ai_fallback_results AS result ON result.id = link.result_id
                    WHERE result.attempt_id = attempt.id
                      AND link.parser_output_id = NEW.parser_output_id
                )
          )
      )
BEGIN
    SELECT RAISE(ABORT, 'AI fallback raw-intake lineage cannot be escaped');
END;
