CREATE TABLE firm_os.organizations (
    org_id          UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    name            TEXT NOT NULL,
    practice_area   TEXT NOT NULL,
    intake_extra    JSONB NOT NULL DEFAULT '{}',
    city            TEXT,
    state           CHAR(2),
    website         TEXT,
    billing_status  TEXT NOT NULL DEFAULT 'trial',
    status          TEXT NOT NULL DEFAULT 'active',
    monthly_sms_budget INTEGER NOT NULL DEFAULT 500,
    default_language TEXT NOT NULL DEFAULT 'en',
    after_hours_en  TEXT,
    after_hours_es  TEXT,
    timezone        TEXT NOT NULL DEFAULT 'America/Chicago',
    twilio_subaccount_sid  TEXT,
    twilio_phone_number    TEXT,
    clio_access_token      TEXT,
    clio_refresh_token     TEXT,
    clio_token_expires_at  TIMESTAMPTZ,
    secret_arn      TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX idx_organizations_status ON firm_os.organizations(status);
CREATE INDEX idx_organizations_billing ON firm_os.organizations(billing_status);
