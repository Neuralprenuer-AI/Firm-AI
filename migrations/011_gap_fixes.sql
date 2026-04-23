-- Migration 011: gap fixes — TCPA opt-out, MessageSid dedup, pytz availability

-- TCPA opt-out flag on contacts
ALTER TABLE firm_os.contacts
    ADD COLUMN IF NOT EXISTS opted_out BOOLEAN NOT NULL DEFAULT FALSE;

CREATE INDEX IF NOT EXISTS idx_contacts_opted_out
    ON firm_os.contacts (org_id, opted_out)
    WHERE opted_out = TRUE;

-- MessageSid deduplication on messages
ALTER TABLE firm_os.messages
    ADD COLUMN IF NOT EXISTS twilio_message_sid VARCHAR(64);

CREATE UNIQUE INDEX IF NOT EXISTS idx_messages_twilio_sid
    ON firm_os.messages (twilio_message_sid)
    WHERE twilio_message_sid IS NOT NULL;
