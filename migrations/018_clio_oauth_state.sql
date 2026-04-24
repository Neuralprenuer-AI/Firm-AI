-- Migration 018: Add clio_oauth_state column for CSRF protection during OAuth flow
-- Required by firmos-clio-oauth-callback Lambda

ALTER TABLE firm_os.organizations
    ADD COLUMN IF NOT EXISTS clio_oauth_state TEXT;
