-- migrations/022_voice_receptionist.sql

-- Rename vapi_assistant_id → elevenlabs_agent_id (conditional)
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'firm_os' AND table_name = 'organizations'
          AND column_name = 'vapi_assistant_id'
    ) THEN
        ALTER TABLE firm_os.organizations
            RENAME COLUMN vapi_assistant_id TO elevenlabs_agent_id;
    ELSE
        ALTER TABLE firm_os.organizations
            ADD COLUMN IF NOT EXISTS elevenlabs_agent_id TEXT;
    END IF;
END $$;

-- Add ElevenLabs voice ID column
ALTER TABLE firm_os.organizations
    ADD COLUMN IF NOT EXISTS elevenlabs_voice_id TEXT DEFAULT '21m00Tcm4TlvDq8ikWAM';
