CREATE TABLE firm_os.escalations (
    escalation_id       UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    org_id              UUID NOT NULL REFERENCES firm_os.organizations(org_id) ON DELETE CASCADE,
    contact_id          UUID NOT NULL REFERENCES firm_os.contacts(contact_id) ON DELETE CASCADE,
    conversation_id     UUID NOT NULL REFERENCES firm_os.conversations(conversation_id) ON DELETE CASCADE,
    triggered_keyword   TEXT NOT NULL,
    severity            TEXT NOT NULL DEFAULT 'high',
    assigned_user_id    UUID REFERENCES firm_os.org_users(user_id),
    status              TEXT NOT NULL DEFAULT 'open',
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    resolved_at         TIMESTAMPTZ
);
CREATE INDEX idx_escalations_org_id ON firm_os.escalations(org_id);
CREATE INDEX idx_escalations_status ON firm_os.escalations(status);
