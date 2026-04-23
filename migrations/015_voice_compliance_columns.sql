BEGIN;

SET search_path TO firm_os, public;

ALTER TABLE organizations ADD COLUMN IF NOT EXISTS after_hours_only BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE organizations ADD COLUMN IF NOT EXISTS greeting_message_en TEXT;
ALTER TABLE organizations ADD COLUMN IF NOT EXISTS greeting_message_es TEXT;
ALTER TABLE organizations ADD COLUMN IF NOT EXISTS after_hours_start TEXT DEFAULT '18:00';
ALTER TABLE organizations ADD COLUMN IF NOT EXISTS after_hours_end TEXT DEFAULT '08:00';
ALTER TABLE organizations ADD COLUMN IF NOT EXISTS escalation_keywords TEXT[] NOT NULL DEFAULT '{}';
ALTER TABLE organizations ADD COLUMN IF NOT EXISTS audit_digest_recipients TEXT[] NOT NULL DEFAULT '{}';

COMMIT;
