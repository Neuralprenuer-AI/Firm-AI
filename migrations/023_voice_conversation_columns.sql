-- migrations/023_voice_conversation_columns.sql
BEGIN;
SET search_path TO firm_os, public;

ALTER TABLE firm_os.conversations
    ADD COLUMN IF NOT EXISTS started_at  TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS ended_at    TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS escalated   BOOLEAN NOT NULL DEFAULT FALSE;

COMMIT;
