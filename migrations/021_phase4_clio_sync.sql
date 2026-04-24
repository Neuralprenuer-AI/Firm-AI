-- Migration 021: Phase 4 full Clio sync tables
BEGIN;
SET search_path TO firm_os, public;

CREATE TABLE IF NOT EXISTS firm_os.clio_notes (
    id                  BIGSERIAL PRIMARY KEY,
    org_id              UUID NOT NULL REFERENCES firm_os.organizations(org_id) ON DELETE CASCADE,
    contact_id          UUID REFERENCES firm_os.contacts(contact_id) ON DELETE SET NULL,
    clio_matter_id      TEXT NOT NULL,
    clio_note_id        TEXT NOT NULL,
    subject             TEXT,
    detail              TEXT,
    note_date           DATE,
    synced_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (org_id, clio_note_id)
);
CREATE INDEX IF NOT EXISTS idx_clio_notes_org_contact ON firm_os.clio_notes(org_id, contact_id);
CREATE INDEX IF NOT EXISTS idx_clio_notes_matter ON firm_os.clio_notes(org_id, clio_matter_id);

CREATE TABLE IF NOT EXISTS firm_os.clio_communications (
    id                  BIGSERIAL PRIMARY KEY,
    org_id              UUID NOT NULL REFERENCES firm_os.organizations(org_id) ON DELETE CASCADE,
    contact_id          UUID REFERENCES firm_os.contacts(contact_id) ON DELETE SET NULL,
    clio_matter_id      TEXT NOT NULL,
    clio_comm_id        TEXT NOT NULL,
    comm_type           TEXT,
    subject             TEXT,
    body                TEXT,
    received_at         TIMESTAMPTZ,
    synced_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (org_id, clio_comm_id)
);
CREATE INDEX IF NOT EXISTS idx_clio_comms_org_contact ON firm_os.clio_communications(org_id, contact_id);
CREATE INDEX IF NOT EXISTS idx_clio_comms_matter ON firm_os.clio_communications(org_id, clio_matter_id);

CREATE TABLE IF NOT EXISTS firm_os.clio_calendar_entries (
    id                  BIGSERIAL PRIMARY KEY,
    org_id              UUID NOT NULL REFERENCES firm_os.organizations(org_id) ON DELETE CASCADE,
    contact_id          UUID REFERENCES firm_os.contacts(contact_id) ON DELETE SET NULL,
    clio_matter_id      TEXT NOT NULL,
    clio_entry_id       TEXT NOT NULL,
    summary             TEXT,
    start_at            TIMESTAMPTZ,
    end_at              TIMESTAMPTZ,
    all_day             BOOLEAN DEFAULT FALSE,
    reminder_sent_48h   BOOLEAN DEFAULT FALSE,
    reminder_sent_24h   BOOLEAN DEFAULT FALSE,
    synced_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (org_id, clio_entry_id)
);
CREATE INDEX IF NOT EXISTS idx_clio_cal_org_contact ON firm_os.clio_calendar_entries(org_id, contact_id);
CREATE INDEX IF NOT EXISTS idx_clio_cal_start ON firm_os.clio_calendar_entries(org_id, start_at);

CREATE TABLE IF NOT EXISTS firm_os.clio_conversations (
    id                  BIGSERIAL PRIMARY KEY,
    org_id              UUID NOT NULL REFERENCES firm_os.organizations(org_id) ON DELETE CASCADE,
    clio_matter_id      TEXT NOT NULL,
    clio_conv_id        TEXT NOT NULL,
    subject             TEXT,
    synced_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (org_id, clio_conv_id)
);
CREATE INDEX IF NOT EXISTS idx_clio_conv_matter ON firm_os.clio_conversations(org_id, clio_matter_id);

CREATE TABLE IF NOT EXISTS firm_os.clio_conversation_messages (
    id                  BIGSERIAL PRIMARY KEY,
    org_id              UUID NOT NULL REFERENCES firm_os.organizations(org_id) ON DELETE CASCADE,
    clio_conv_id        TEXT NOT NULL,
    clio_msg_id         TEXT NOT NULL,
    body                TEXT,
    author_name         TEXT,
    created_at          TIMESTAMPTZ,
    synced_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (org_id, clio_msg_id),
    CONSTRAINT fk_clio_msg_conv FOREIGN KEY (org_id, clio_conv_id)
        REFERENCES firm_os.clio_conversations(org_id, clio_conv_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_clio_msg_conv ON firm_os.clio_conversation_messages(org_id, clio_conv_id);

CREATE TABLE IF NOT EXISTS firm_os.clio_webhook_subscriptions (
    id                  BIGSERIAL PRIMARY KEY,
    org_id              UUID NOT NULL REFERENCES firm_os.organizations(org_id) ON DELETE CASCADE,
    clio_webhook_id     TEXT NOT NULL,
    model               TEXT NOT NULL,
    url                 TEXT NOT NULL,
    expires_at          TIMESTAMPTZ NOT NULL,
    hook_secret         TEXT, -- stored as received from Clio handshake; internal VPC table only
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (org_id, clio_webhook_id)
);
CREATE INDEX IF NOT EXISTS idx_clio_webhooks_org ON firm_os.clio_webhook_subscriptions(org_id);
CREATE INDEX IF NOT EXISTS idx_clio_webhooks_expires ON firm_os.clio_webhook_subscriptions(expires_at);

COMMIT;
