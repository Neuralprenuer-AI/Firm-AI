CREATE TABLE firm_os.intake_records (
    intake_id       UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    org_id          UUID NOT NULL REFERENCES firm_os.organizations(org_id) ON DELETE CASCADE,
    conversation_id UUID NOT NULL REFERENCES firm_os.conversations(conversation_id) ON DELETE CASCADE,
    contact_id      UUID NOT NULL REFERENCES firm_os.contacts(contact_id) ON DELETE CASCADE,
    data            JSONB NOT NULL DEFAULT '{}',
    summary_en      TEXT,
    clio_matter_id  TEXT,
    clio_note_id    TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX idx_intake_records_org_id ON firm_os.intake_records(org_id);
CREATE INDEX idx_intake_records_contact_id ON firm_os.intake_records(contact_id);
