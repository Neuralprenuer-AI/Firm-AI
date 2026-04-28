-- migrations/025_callback_reminders.sql
BEGIN;

SET search_path TO firm_os, public;

CREATE TABLE IF NOT EXISTS firm_os.callback_reminders (
    reminder_id     UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id          UUID NOT NULL REFERENCES firm_os.organizations(org_id) ON DELETE CASCADE,
    contact_id      UUID NOT NULL REFERENCES firm_os.contacts(contact_id) ON DELETE CASCADE,
    conversation_id UUID REFERENCES firm_os.conversations(conversation_id) ON DELETE SET NULL,
    contact_name    TEXT,
    contact_phone   TEXT,
    note            TEXT,
    status          TEXT NOT NULL DEFAULT 'pending',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at    TIMESTAMPTZ,
    CONSTRAINT chk_reminder_status CHECK (status IN ('pending', 'completed', 'dismissed'))
);

CREATE INDEX IF NOT EXISTS idx_reminders_org_status
    ON firm_os.callback_reminders(org_id, status);

CREATE INDEX IF NOT EXISTS idx_reminders_contact
    ON firm_os.callback_reminders(contact_id);

COMMIT;
