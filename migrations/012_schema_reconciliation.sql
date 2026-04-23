-- =====================================================================
-- Migration: 012_schema_reconciliation.sql
-- Purpose:   Non-destructive reconciliation of deployed schema vs spec.
--            Adds alias columns + backfills so existing Lambdas (using
--            OLD column names) continue to work while new code can begin
--            adopting the NEW column names. NO columns are dropped.
--
-- Priority:  DO NOT break production. All existing Lambdas read/write
--            the OLD columns. Dropping/renaming is deferred to a later
--            migration once all Lambdas are cut over.
--
-- Properties: Idempotent (re-runnable). Uses IF NOT EXISTS everywhere.
--             No destructive operations (no DROP, no column rename).
--
-- Author:    AI Firm OS / Phase 1 Schema Reconciliation
-- =====================================================================

BEGIN;

SET search_path TO firm_os, public;

-- ---------------------------------------------------------------------
-- (a) firm_os.contacts
--     - phone              -> add alias phone_e164 (E.164 normalized)
--     - intake_status      -> add alias contact_status (broader lifecycle)
-- ---------------------------------------------------------------------

-- phone_e164 alias column
ALTER TABLE firm_os.contacts
    ADD COLUMN IF NOT EXISTS phone_e164 TEXT;

-- Backfill phone_e164 from existing phone column (only where empty)
UPDATE firm_os.contacts
   SET phone_e164 = phone
 WHERE phone_e164 IS NULL
   AND phone IS NOT NULL;

-- Unique per-org index on phone_e164 (partial, ignores NULLs)
CREATE UNIQUE INDEX IF NOT EXISTS idx_contacts_phone_e164
    ON firm_os.contacts (org_id, phone_e164)
    WHERE phone_e164 IS NOT NULL;

-- contact_status alias column
ALTER TABLE firm_os.contacts
    ADD COLUMN IF NOT EXISTS contact_status TEXT;

-- Backfill contact_status from existing intake_status
UPDATE firm_os.contacts
   SET contact_status = intake_status
 WHERE contact_status IS NULL
   AND intake_status IS NOT NULL;

-- Check constraint on contact_status (idempotent via DO block since
-- Postgres ALTER TABLE does not support IF NOT EXISTS for CHECK constraints
-- on all supported versions; emulate it).
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
          FROM pg_constraint
         WHERE conname = 'chk_contact_status'
           AND conrelid = 'firm_os.contacts'::regclass
    ) THEN
        ALTER TABLE firm_os.contacts
            ADD CONSTRAINT chk_contact_status
            CHECK (
                contact_status IS NULL
                OR contact_status IN (
                    'new',
                    'open_lead',
                    'intake_complete',
                    'active_client',
                    'archived'
                )
            );
    END IF;
END
$$;


-- ---------------------------------------------------------------------
-- (b) firm_os.organizations
--     - twilio_phone_number (single)
--         -> add intake_phone_number + status_phone_number
--     - add mandatory_disclaimer (used by intake-agent for ABA override)
-- ---------------------------------------------------------------------

ALTER TABLE firm_os.organizations
    ADD COLUMN IF NOT EXISTS intake_phone_number TEXT;

ALTER TABLE firm_os.organizations
    ADD COLUMN IF NOT EXISTS status_phone_number TEXT;

-- Backfill intake_phone_number from legacy single-column twilio_phone_number.
-- Assumption: existing single numbers are intake lines (per spec note).
UPDATE firm_os.organizations
   SET intake_phone_number = twilio_phone_number
 WHERE intake_phone_number IS NULL
   AND twilio_phone_number IS NOT NULL;

-- Unique indexes (partial, NULL-safe) on both phone-line columns.
CREATE UNIQUE INDEX IF NOT EXISTS idx_organizations_intake_phone_number
    ON firm_os.organizations (intake_phone_number)
    WHERE intake_phone_number IS NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS idx_organizations_status_phone_number
    ON firm_os.organizations (status_phone_number)
    WHERE status_phone_number IS NOT NULL;

-- Mandatory disclaimer (ABA attorney-advertising override text)
ALTER TABLE firm_os.organizations
    ADD COLUMN IF NOT EXISTS mandatory_disclaimer TEXT;


-- ---------------------------------------------------------------------
-- (c) firm_os.conversations
--     - state -> add alias status (text lifecycle)
--     - add channel (sms / voice / web ...)
--     - add turn_count (INT NOT NULL DEFAULT 0) if missing
-- ---------------------------------------------------------------------

ALTER TABLE firm_os.conversations
    ADD COLUMN IF NOT EXISTS status TEXT;

ALTER TABLE firm_os.conversations
    ADD COLUMN IF NOT EXISTS channel TEXT;

-- Backfill status from legacy state column
UPDATE firm_os.conversations
   SET status = state
 WHERE status IS NULL
   AND state IS NOT NULL;

-- Default channel to 'sms' for all existing rows (Phase 1 is SMS-only)
UPDATE firm_os.conversations
   SET channel = 'sms'
 WHERE channel IS NULL;

-- turn_count (g) - required by router + intake-agent turn-limit logic
ALTER TABLE firm_os.conversations
    ADD COLUMN IF NOT EXISTS turn_count INT NOT NULL DEFAULT 0;


-- ---------------------------------------------------------------------
-- (d) firm_os.messages
--     - direction (inbound/outbound) -> add alias role (user/assistant/...)
-- ---------------------------------------------------------------------

ALTER TABLE firm_os.messages
    ADD COLUMN IF NOT EXISTS role TEXT;

-- Backfill role from direction
--   inbound  -> 'user'
--   outbound -> 'assistant'
-- system/tool roles will be populated by new code paths going forward.
UPDATE firm_os.messages
   SET role = CASE
                  WHEN direction = 'inbound'  THEN 'user'
                  WHEN direction = 'outbound' THEN 'assistant'
                  ELSE NULL
              END
 WHERE role IS NULL
   AND direction IS NOT NULL;


-- ---------------------------------------------------------------------
-- (e) firm_os.organizations (continued)
--     - secret_arn -> add alias anthropic_key_secret_arn
--       (spec naming is explicit about which vendor the secret is for)
-- ---------------------------------------------------------------------

ALTER TABLE firm_os.organizations
    ADD COLUMN IF NOT EXISTS anthropic_key_secret_arn TEXT;

-- Backfill anthropic_key_secret_arn from legacy secret_arn.
-- NOTE: this assumes all existing secret_arns are for Anthropic keys,
-- which is the Phase 1 invariant. If that changes, revisit this backfill.
UPDATE firm_os.organizations
   SET anthropic_key_secret_arn = secret_arn
 WHERE anthropic_key_secret_arn IS NULL
   AND secret_arn IS NOT NULL;


-- ---------------------------------------------------------------------
-- (f) firm_os.audit_digests (NEW TABLE)
--     Consumed by firmos-audit-digest Lambda (daily rollup per org).
-- ---------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS firm_os.audit_digests (
    digest_id             UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id                UUID        NOT NULL
                                      REFERENCES firm_os.organizations(org_id)
                                      ON DELETE RESTRICT,
    digest_date           DATE        NOT NULL,
    total_conversations   INT         NOT NULL DEFAULT 0,
    total_escalations     INT         NOT NULL DEFAULT 0,
    flags_count           INT         NOT NULL DEFAULT 0,
    summary_html          TEXT,
    created_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (org_id, digest_date)
);

CREATE INDEX IF NOT EXISTS idx_audit_digests_org
    ON firm_os.audit_digests (org_id, digest_date DESC);


-- ---------------------------------------------------------------------
-- Post-migration sanity checks (informational only; no failures raised)
-- ---------------------------------------------------------------------

DO $$
DECLARE
    v_contacts_backfill_gaps INT;
    v_orgs_backfill_gaps     INT;
    v_conv_backfill_gaps     INT;
    v_msgs_backfill_gaps     INT;
BEGIN
    SELECT COUNT(*) INTO v_contacts_backfill_gaps
      FROM firm_os.contacts
     WHERE phone IS NOT NULL AND phone_e164 IS NULL;

    SELECT COUNT(*) INTO v_orgs_backfill_gaps
      FROM firm_os.organizations
     WHERE twilio_phone_number IS NOT NULL
       AND intake_phone_number IS NULL;

    SELECT COUNT(*) INTO v_conv_backfill_gaps
      FROM firm_os.conversations
     WHERE state IS NOT NULL AND status IS NULL;

    SELECT COUNT(*) INTO v_msgs_backfill_gaps
      FROM firm_os.messages
     WHERE direction IS NOT NULL AND role IS NULL;

    RAISE NOTICE 'Migration 012 backfill residuals — contacts:% orgs:% conversations:% messages:%',
        v_contacts_backfill_gaps,
        v_orgs_backfill_gaps,
        v_conv_backfill_gaps,
        v_msgs_backfill_gaps;
END
$$;

COMMIT;

-- =====================================================================
-- End of migration 012_schema_reconciliation.sql
-- Next step (separate PR): migrate each Lambda to read/write the new
-- columns, then schedule migration 013 to drop the legacy aliases.
-- =====================================================================
