CREATE TABLE firm_os.conversations (
    conversation_id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    org_id          UUID NOT NULL REFERENCES firm_os.organizations(org_id) ON DELETE CASCADE,
    contact_id      UUID NOT NULL REFERENCES firm_os.contacts(contact_id) ON DELETE CASCADE,
    state           TEXT NOT NULL DEFAULT 'language_pending',
    turn_count      INTEGER NOT NULL DEFAULT 0,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX idx_conversations_org_id ON firm_os.conversations(org_id);
CREATE INDEX idx_conversations_contact_id ON firm_os.conversations(contact_id);
CREATE INDEX idx_conversations_state ON firm_os.conversations(state);
