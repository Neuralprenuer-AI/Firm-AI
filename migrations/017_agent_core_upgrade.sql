-- 017_agent_core_upgrade.sql
-- Adds firm_profile JSONB, extended conversation state machine, state log,
-- contact memory fields, and intake_records dispatcher columns.
-- Seeds Vega Immigration Law firm_profile for testing.
-- Idempotent: safe to re-run.

BEGIN;
SET search_path TO firm_os, public;

-- ---------------------------------------------------------------------------
-- 1. organizations.firm_profile
-- ---------------------------------------------------------------------------
ALTER TABLE firm_os.organizations
    ADD COLUMN IF NOT EXISTS firm_profile JSONB NOT NULL DEFAULT '{}'::jsonb;

-- ---------------------------------------------------------------------------
-- 2. conversations — extended state machine columns
-- (turn_count already exists from 005_conversations.sql — skip)
-- ---------------------------------------------------------------------------
ALTER TABLE firm_os.conversations
    ADD COLUMN IF NOT EXISTS mode             TEXT        NOT NULL DEFAULT 'faq',
    ADD COLUMN IF NOT EXISTS previous_state   TEXT,
    ADD COLUMN IF NOT EXISTS state_updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    ADD COLUMN IF NOT EXISTS last_message_at  TIMESTAMPTZ;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'conversations_mode_check'
    ) THEN
        ALTER TABLE firm_os.conversations
            ADD CONSTRAINT conversations_mode_check
            CHECK (mode IN ('emergency','intake','faq','returning','closed'));
    END IF;
END$$;

-- ---------------------------------------------------------------------------
-- 3. conversation_state_log — append-only audit of every state transition
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS firm_os.conversation_state_log (
    id               UUID        PRIMARY KEY DEFAULT uuid_generate_v4(),
    org_id           UUID        NOT NULL REFERENCES firm_os.organizations(org_id) ON DELETE CASCADE,
    conversation_id  UUID        NOT NULL REFERENCES firm_os.conversations(conversation_id) ON DELETE CASCADE,
    from_state       TEXT,
    to_state         TEXT        NOT NULL,
    mode             TEXT        NOT NULL,
    next_action      TEXT        NOT NULL,
    reasoning        TEXT,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------------
-- 4. contacts — memory fields for returning-client mode
-- ---------------------------------------------------------------------------
ALTER TABLE firm_os.contacts
    ADD COLUMN IF NOT EXISTS profile_summary     JSONB       NOT NULL DEFAULT '{}'::jsonb,
    ADD COLUMN IF NOT EXISTS last_intake_at      TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS last_contact_at     TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS total_conversations INTEGER     NOT NULL DEFAULT 0;

-- ---------------------------------------------------------------------------
-- 5. intake_records — dispatcher columns
-- (conversation_id and org_id already exist — only add new columns)
-- ---------------------------------------------------------------------------
ALTER TABLE firm_os.intake_records
    ADD COLUMN IF NOT EXISTS status             TEXT        NOT NULL DEFAULT 'in_progress',
    ADD COLUMN IF NOT EXISTS fields             JSONB       NOT NULL DEFAULT '{}'::jsonb,
    ADD COLUMN IF NOT EXISTS completion_percent INTEGER     NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS full_name          TEXT,
    ADD COLUMN IF NOT EXISTS phone_verified     TEXT,
    ADD COLUMN IF NOT EXISTS preferred_language TEXT,
    ADD COLUMN IF NOT EXISTS case_type          TEXT,
    ADD COLUMN IF NOT EXISTS urgency            TEXT,
    ADD COLUMN IF NOT EXISTS detention_status   TEXT,
    ADD COLUMN IF NOT EXISTS brief_description  TEXT,
    ADD COLUMN IF NOT EXISTS updated_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    ADD COLUMN IF NOT EXISTS closed_at          TIMESTAMPTZ;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'intake_records_status_check'
    ) THEN
        ALTER TABLE firm_os.intake_records
            ADD CONSTRAINT intake_records_status_check
            CHECK (status IN ('in_progress','submitted','reviewed','converted','abandoned'));
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'intake_records_detention_status_check'
    ) THEN
        ALTER TABLE firm_os.intake_records
            ADD CONSTRAINT intake_records_detention_status_check
            CHECK (detention_status IS NULL OR detention_status IN ('free','detained','family_detained'));
    END IF;
END$$;

-- ---------------------------------------------------------------------------
-- 6. Indexes
-- ---------------------------------------------------------------------------
CREATE INDEX IF NOT EXISTS idx_conv_state_log_org_conv
    ON firm_os.conversation_state_log (org_id, conversation_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_conv_state_log_created
    ON firm_os.conversation_state_log (created_at DESC);

CREATE INDEX IF NOT EXISTS idx_conversations_org_state
    ON firm_os.conversations (org_id, state);

CREATE INDEX IF NOT EXISTS idx_conversations_last_message_at
    ON firm_os.conversations (org_id, last_message_at DESC NULLS LAST);

CREATE INDEX IF NOT EXISTS idx_contacts_org_last_contact
    ON firm_os.contacts (org_id, last_contact_at DESC NULLS LAST);

CREATE INDEX IF NOT EXISTS idx_intake_records_org_conv
    ON firm_os.intake_records (org_id, conversation_id);

CREATE INDEX IF NOT EXISTS idx_intake_records_org_status
    ON firm_os.intake_records (org_id, status);

CREATE INDEX IF NOT EXISTS idx_organizations_firm_profile_gin
    ON firm_os.organizations USING gin (firm_profile jsonb_path_ops);

-- ---------------------------------------------------------------------------
-- 7. Seed: Vega Immigration Law — firm_profile
-- org_id: a1b2c3d4-0002-0002-0002-000000000002
-- ---------------------------------------------------------------------------
UPDATE firm_os.organizations
   SET firm_profile = '{
        "firm_name": "Vega Immigration Law",
        "practice_areas": ["asylum","family petition","DACA","removal defense","citizenship","visa","TPS"],
        "languages_supported": ["en", "es"],
        "consultation_fee": "$150 initial consult (credited toward retainer if hired)",
        "hours": {
            "monday":    "08:30-17:30",
            "tuesday":   "08:30-17:30",
            "wednesday": "08:30-17:30",
            "thursday":  "08:30-17:30",
            "friday":    "08:30-16:00",
            "saturday":  null,
            "sunday":    null,
            "timezone":  "America/Chicago"
        },
        "phone": "+17135550188",
        "email": "intake@vegaimmigration.law",
        "website": "https://vegaimmigration.law",
        "address": "11511 Katy Fwy, Suite 650, Houston, TX 77079",
        "attorneys": [
            {
                "name": "Maria Vega, Esq.",
                "phone": "+17135550189",
                "email": "maria@vegaimmigration.law",
                "languages": ["en","es"],
                "practice_areas": ["asylum","removal defense","family petition"],
                "is_primary": true
            },
            {
                "name": "Diego Ortiz, Esq.",
                "phone": "+17135550190",
                "email": "diego@vegaimmigration.law",
                "languages": ["en","es"],
                "practice_areas": ["citizenship","DACA","TPS","visa"],
                "is_primary": false
            }
        ],
        "faqs": [
            {
                "question_id": "hours",
                "question": "What are your hours?",
                "answer": "We are open Mon-Thu 8:30am-5:30pm and Fri 8:30am-4:00pm Central. Closed weekends.",
                "category": "logistics"
            },
            {
                "question_id": "consult_fee",
                "question": "How much is a consultation?",
                "answer": "Initial consultations are $150 and that amount is credited toward your retainer if you hire us.",
                "category": "fees"
            },
            {
                "question_id": "languages",
                "question": "Do you speak Spanish?",
                "answer": "Yes — both of our attorneys and our entire intake team are fully bilingual in English and Spanish.",
                "category": "logistics"
            },
            {
                "question_id": "detention",
                "question": "My family member was detained — can you help?",
                "answer": "Yes. Detention cases are our top priority. An attorney will call you back within 30 minutes day or night.",
                "category": "services"
            },
            {
                "question_id": "practice_areas",
                "question": "What kind of immigration cases do you handle?",
                "answer": "Asylum, family petitions, DACA, removal defense, citizenship, visas, and TPS.",
                "category": "services"
            },
            {
                "question_id": "payment_plans",
                "question": "Do you offer payment plans?",
                "answer": "Yes, we offer flexible payment plans for most case types. An attorney will discuss options during your consult.",
                "category": "fees"
            }
        ],
        "emergency_callback_minutes": 30,
        "disclaimer_text": "This is Maria, an SMS assistant for Vega Immigration Law. I am not an attorney and cannot give legal advice. Msg&data rates may apply. Reply STOP to opt out."
   }'::jsonb
 WHERE org_id = 'a1b2c3d4-0002-0002-0002-000000000002';

COMMIT;
