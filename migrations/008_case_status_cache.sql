CREATE TABLE firm_os.case_status_cache (
    cache_id        UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    org_id          UUID NOT NULL REFERENCES firm_os.organizations(org_id) ON DELETE CASCADE,
    contact_id      UUID NOT NULL REFERENCES firm_os.contacts(contact_id) ON DELETE CASCADE,
    clio_matter_id  TEXT NOT NULL,
    status_text_en  TEXT NOT NULL,
    status_text_es  TEXT,
    cached_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(org_id, clio_matter_id)
);
CREATE INDEX idx_case_status_cache_org_id ON firm_os.case_status_cache(org_id);
