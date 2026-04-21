CREATE TABLE firm_os.messages (
    message_id      UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    org_id          UUID NOT NULL REFERENCES firm_os.organizations(org_id) ON DELETE CASCADE,
    conversation_id UUID NOT NULL REFERENCES firm_os.conversations(conversation_id) ON DELETE CASCADE,
    direction       TEXT NOT NULL,
    body            TEXT NOT NULL,
    channel         TEXT NOT NULL DEFAULT 'sms',
    twilio_sid      TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX idx_messages_org_id ON firm_os.messages(org_id);
CREATE INDEX idx_messages_conversation_id ON firm_os.messages(conversation_id);
