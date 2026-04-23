-- Migration 014: Add CRM and Clio token columns to firm_os.organizations
-- Required by firmos-crm-push (crm_platform, clio_access_token, clio_token_expires_at)
-- Required by firmos-clio-sync (crm_platform, clio_access_token, clio_refresh_token,
--                                clio_token_expires_at, secret_arn)

ALTER TABLE firm_os.organizations
    ADD COLUMN IF NOT EXISTS crm_platform TEXT
        CHECK (crm_platform IN ('clio', 'lawmatics', 'filevine') OR crm_platform IS NULL);

ALTER TABLE firm_os.organizations
    ADD COLUMN IF NOT EXISTS crm_credentials_secret_arn TEXT;

ALTER TABLE firm_os.organizations
    ADD COLUMN IF NOT EXISTS clio_token_secret_arn TEXT;

ALTER TABLE firm_os.organizations
    ADD COLUMN IF NOT EXISTS clio_access_token TEXT;

ALTER TABLE firm_os.organizations
    ADD COLUMN IF NOT EXISTS clio_refresh_token TEXT;

ALTER TABLE firm_os.organizations
    ADD COLUMN IF NOT EXISTS clio_token_expires_at TIMESTAMPTZ;
