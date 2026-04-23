-- =====================================================================
-- Migration: 013_organizations_missing_columns.sql
-- Purpose:   Add columns that were defined in the spec (§5.1) and
--            referenced by multiple Lambdas (firmos-org-setup,
--            firmos-audit-digest, firmos-vapi-webhook,
--            firmos-vapi-functions, firmos-onboard-firm) but never
--            included in migration 002_organizations.sql.
--
--            This migration is the root-cause fix for the
--            "column partner_email does not exist" runtime error in
--            firmos-audit-digest and related Lambdas.
--
-- Properties: Idempotent. All ADD COLUMN statements use IF NOT EXISTS.
--             No destructive operations. Existing rows unaffected.
--
-- Columns added to firm_os.organizations:
--   partner_email           TEXT  — managing partner contact email
--   agent_display_name      TEXT  — display name shown to contacts
--   emergency_contact_number TEXT — fallback escalation phone number
--   monthly_token_budget    INT   — Gemini token spend cap per month
--   vapi_assistant_id       TEXT  — Vapi assistant UUID (unique per org)
--   vapi_phone_number       TEXT  — Vapi-provisioned inbound phone number
--
-- Author:    AI Firm OS / Schema Fix — 2026-04-23
-- =====================================================================

BEGIN;

SET search_path TO firm_os, public;

-- Managing partner email — used as primary digest recipient and
-- welcome-email destination during onboarding.
ALTER TABLE firm_os.organizations
    ADD COLUMN IF NOT EXISTS partner_email TEXT;

-- Human-readable display name for the AI agent shown in SMS/voice
-- interactions (e.g. "Alex from Smith Law").
ALTER TABLE firm_os.organizations
    ADD COLUMN IF NOT EXISTS agent_display_name TEXT;

-- Fallback phone number for live-transfer escalations when no
-- on-call org_user is available.
ALTER TABLE firm_os.organizations
    ADD COLUMN IF NOT EXISTS emergency_contact_number TEXT;

-- Monthly Gemini token budget cap. 0 = unlimited (not recommended).
-- Default 100000 matches the Phase 1 billing assumption.
ALTER TABLE firm_os.organizations
    ADD COLUMN IF NOT EXISTS monthly_token_budget INTEGER NOT NULL DEFAULT 100000;

-- Vapi assistant ID — uniquely identifies the AI voice assistant
-- provisioned for this org. Used as the lookup key in vapi-webhook
-- and vapi-functions.
ALTER TABLE firm_os.organizations
    ADD COLUMN IF NOT EXISTS vapi_assistant_id TEXT;

CREATE UNIQUE INDEX IF NOT EXISTS idx_organizations_vapi_assistant_id
    ON firm_os.organizations (vapi_assistant_id)
    WHERE vapi_assistant_id IS NOT NULL;

-- Vapi-provisioned inbound phone number for voice intake.
ALTER TABLE firm_os.organizations
    ADD COLUMN IF NOT EXISTS vapi_phone_number TEXT;

CREATE UNIQUE INDEX IF NOT EXISTS idx_organizations_vapi_phone_number
    ON firm_os.organizations (vapi_phone_number)
    WHERE vapi_phone_number IS NOT NULL;


-- ---------------------------------------------------------------------
-- Post-migration sanity check (informational only)
-- ---------------------------------------------------------------------

DO $$
DECLARE
    v_col_count INT;
BEGIN
    SELECT COUNT(*)
      INTO v_col_count
      FROM information_schema.columns
     WHERE table_schema = 'firm_os'
       AND table_name   = 'organizations'
       AND column_name  IN (
           'partner_email',
           'agent_display_name',
           'emergency_contact_number',
           'monthly_token_budget',
           'vapi_assistant_id',
           'vapi_phone_number'
       );

    IF v_col_count < 6 THEN
        RAISE WARNING 'Migration 013 sanity check: expected 6 new columns, found %', v_col_count;
    ELSE
        RAISE NOTICE 'Migration 013 complete — all 6 columns verified on firm_os.organizations';
    END IF;
END
$$;

COMMIT;

-- =====================================================================
-- End of migration 013_organizations_missing_columns.sql
-- =====================================================================
