CREATE TABLE firm_os.org_users (
    user_id         UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    org_id          UUID NOT NULL REFERENCES firm_os.organizations(org_id) ON DELETE CASCADE,
    supabase_uid    UUID UNIQUE,
    name            TEXT NOT NULL,
    email           TEXT NOT NULL,
    org_role        TEXT NOT NULL DEFAULT 'associate',
    escalation_routing BOOLEAN NOT NULL DEFAULT FALSE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX idx_org_users_org_id ON firm_os.org_users(org_id);
CREATE INDEX idx_org_users_supabase_uid ON firm_os.org_users(supabase_uid);
