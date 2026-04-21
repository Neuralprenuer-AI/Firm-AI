CREATE TABLE firm_os.contacts (
    contact_id          UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    org_id              UUID NOT NULL REFERENCES firm_os.organizations(org_id) ON DELETE CASCADE,
    phone               TEXT NOT NULL,
    name                TEXT,
    preferred_language  TEXT NOT NULL DEFAULT 'en',
    intake_status       TEXT NOT NULL DEFAULT 'new',
    clio_contact_id     TEXT,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(org_id, phone)
);
CREATE INDEX idx_contacts_org_id ON firm_os.contacts(org_id);
CREATE INDEX idx_contacts_phone ON firm_os.contacts(phone);
