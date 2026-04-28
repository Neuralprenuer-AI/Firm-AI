-- migrations/026_intake_quality_score.sql
BEGIN;

SET search_path TO firm_os, public;

ALTER TABLE firm_os.intake_records
    ADD COLUMN IF NOT EXISTS quality_score   INTEGER,
    ADD COLUMN IF NOT EXISTS quality_flags   TEXT[]  DEFAULT '{}';

COMMENT ON COLUMN firm_os.intake_records.quality_score IS
    'AI intake completeness score 0-100. 25pts each: has_name, has_issue, has_summary, has_language.';

COMMENT ON COLUMN firm_os.intake_records.quality_flags IS
    'Array of missing field keys, e.g. {has_name,has_issue}.';

COMMIT;
