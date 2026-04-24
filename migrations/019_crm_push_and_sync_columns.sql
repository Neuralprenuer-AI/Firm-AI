-- Migration 019: Add missing CRM columns to intake_records and case_status_cache
-- Required by firmos-crm-push (crm_pushed, crm_pushed_at, crm_matter_id, crm_push_error)
-- Required by firmos-clio-sync  (matter_display_number, matter_status, notes_text,
--                                 notes_hash, last_synced_at)
-- Also relaxes case_status_cache.status_text_en (was NOT NULL, never populated by sync)
-- Idempotent: safe to re-run.

BEGIN;
SET search_path TO firm_os, public;

-- ---------------------------------------------------------------------------
-- 1. intake_records — CRM push tracking
-- ---------------------------------------------------------------------------
ALTER TABLE firm_os.intake_records
    ADD COLUMN IF NOT EXISTS crm_pushed     BOOLEAN     NOT NULL DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS crm_pushed_at  TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS crm_matter_id  TEXT,
    ADD COLUMN IF NOT EXISTS crm_push_error TEXT;

CREATE INDEX IF NOT EXISTS idx_intake_records_crm_pushed
    ON firm_os.intake_records (org_id, crm_pushed)
    WHERE crm_pushed = FALSE;

-- ---------------------------------------------------------------------------
-- 2. case_status_cache — sync columns + relax legacy NOT NULL
-- ---------------------------------------------------------------------------
ALTER TABLE firm_os.case_status_cache
    ADD COLUMN IF NOT EXISTS matter_display_number TEXT,
    ADD COLUMN IF NOT EXISTS matter_status         TEXT,
    ADD COLUMN IF NOT EXISTS notes_text            TEXT,
    ADD COLUMN IF NOT EXISTS notes_hash            TEXT,
    ADD COLUMN IF NOT EXISTS last_synced_at        TIMESTAMPTZ;

-- status_text_en was NOT NULL with no default; sync never sends it — relax to nullable
ALTER TABLE firm_os.case_status_cache
    ALTER COLUMN status_text_en DROP NOT NULL;

-- backfill last_synced_at from cached_at for any existing rows
UPDATE firm_os.case_status_cache
   SET last_synced_at = cached_at
 WHERE last_synced_at IS NULL AND cached_at IS NOT NULL;

COMMIT;
