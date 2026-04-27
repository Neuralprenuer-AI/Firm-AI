# Voice Receptionist — Design Spec

**Date:** 2026-04-27
**Status:** Approved
**Stack:** Twilio Voice + ElevenLabs Conversational AI + Gemini + Clio

---

## Goal

Add a full AI voice receptionist to every law firm on the platform. The receptionist answers all inbound calls (during business hours and after-hours), conducts intake for new callers, checks case status for existing clients, books appointments directly in Clio, and live-transfers to an on-call attorney for emergencies or at caller request. No attorney needed unless truly required.

---

## Architecture

### Call Flow

```
Client calls firm's Twilio number
    → Twilio routes to ElevenLabs ConvAI agent (native Twilio integration)
    → ElevenLabs handles full conversation: STT (Scribe) + LLM + TTS (streaming)
    → Mid-call: agent calls firmos-voice-tools endpoints (API GW) for live data
        lookup_caller      → existing client? pull Clio case status
        complete_intake    → write intake_records + push to Clio
        check_availability → query clio_calendar_entries for open slots
        book_appointment   → create Clio calendar entry + DB row
        escalate_transfer  → return on-call attorney phone → ElevenLabs live transfers
    → Call ends → ElevenLabs post-call webhook → firmos-voice-webhook
        → stores full transcript to messages (channel='voice')
        → creates conversations row
        → invokes firmos-escalation if emergency flagged
```

### Why ElevenLabs ConvAI (not custom Twilio loop)

- Streaming TTS — no audio generation gaps, genuinely human-sounding
- Built-in STT (Scribe) — higher accuracy than Twilio's native speech recognition
- Native Twilio phone integration — no TwiML webhook loop to maintain
- Handles interruptions, barge-in, bilingual mid-call switching natively
- Tool calling built in — agent calls our API endpoints during conversation
- Post-call webhook with full transcript + metadata
- Law firm intake templates available as starting point

---

## ElevenLabs Agent Configuration

One agent provisioned per firm via ElevenLabs API at onboarding. Stored as `organizations.elevenlabs_agent_id`.

### System Prompt Template

```
You are Alex, the AI receptionist for {firm_name}, a {practice_area} law firm.

ROLE: You handle new client intake, case status checks, appointment scheduling,
and emergency escalation. You are NOT an attorney. Never give legal advice,
cite case law, predict outcomes, or discuss fees.

MANDATORY DISCLAIMER: On every call, state this before anything else:
"I'm an AI assistant, not an attorney. No attorney-client relationship is formed
until confirmed in writing by a licensed attorney."

LANGUAGE: Respond in English by default. If the caller speaks Spanish at any point,
switch to Spanish for the remainder of the call.

BUSINESS HOURS: {timezone}, {hours}. After hours: still handle intake and scheduling,
but note that an attorney will follow up next business day.

FIRM CONTEXT: {firm_name} | Practice area: {practice_area} | Agent name: {agent_display_name}

CALL FLOW:
1. Answer → disclaimer → "How can I help you today?"
2. Use lookup_caller to check if this is an existing client.
   - Existing client: provide case status, offer to schedule or answer questions.
   - New caller: run full intake (issue, date, name, has attorney).
3. Appointment request: use check_availability then book_appointment.
4. Emergency or explicit attorney request: use escalate_transfer immediately.
   Say: "Let me connect you with an attorney right now."

ESCALATION CRITERIA (use escalate_transfer):
- Caller mentions arrest, detention, ICE, injury, immediate court date, safety risk
- Caller explicitly asks to speak to an attorney
- Any situation requiring immediate legal action

DO NOT ESCALATE for: routine questions, scheduling, general frustration, past events.
```

### Tools (5 endpoints)

| Tool name | Method | Route | Purpose |
|---|---|---|---|
| `lookup_caller` | GET | `/firmos/voice/caller` | Find contact, get Clio case status |
| `complete_intake` | POST | `/firmos/voice/intake` | Create intake_records + invoke firmos-crm-push |
| `check_availability` | GET | `/firmos/voice/availability` | Query clio_calendar_entries for open slots |
| `book_appointment` | POST | `/firmos/voice/appointment` | Create Clio calendar entry + DB row |
| `escalate_transfer` | POST | `/firmos/voice/escalate` | Return on-call attorney phone for live transfer |

### Voice

ElevenLabs professional bilingual voice (default: Rachel). Firm can change in `/settings/voice` dashboard page. ElevenLabs voice ID stored in `organizations.elevenlabs_voice_id`.

---

## Lambdas

### `firmos-voice-tools` (NEW)

**Trigger:** API Gateway — called by ElevenLabs mid-conversation  
**Timeout:** 10s (caller is waiting — every endpoint must respond under 500ms)  
**Auth:** `X-Voice-Secret` header checked against `firmos/voice/webhook-secret` in Secrets Manager. NOT JWT — ElevenLabs is the caller, not a dashboard user.  
**Layer:** firmos-shared:14

**Endpoints:**

`GET /firmos/voice/caller?phone={e164}&org_id={uuid}`
- Normalize phone to E.164
- Look up contact in `firm_os.contacts`
- If found: pull from `case_status_cache` + `clio_calendar_entries` (next upcoming)
- Returns: `{is_existing_client, contact_id, name, case_status, upcoming_appointment}`
- If not found: returns `{is_existing_client: false}`

`POST /firmos/voice/intake`
```json
{
  "org_id": "...",
  "phone": "+1...",
  "name": "Carlos Ramirez",
  "issue": "Immigration detention hold",
  "incident_date": "last Thursday",
  "has_attorney": false,
  "language": "en"
}
```
- Upsert contact (create if new, update name if existing)
- Insert `intake_records` row with `is_complete=true`
- Async invoke `firmos-crm-push` to create Clio contact + matter
- Returns: `{contact_id, intake_id, success: true}`

`GET /firmos/voice/availability?org_id={uuid}&date={YYYY-MM-DD}`
- Query `clio_calendar_entries` for entries on that date for the org
- Return booked slots so agent can suggest open windows
- Returns: `{booked_slots: ["09:00", "14:00"], suggested_open: ["10:00", "11:00", "15:00"]}`

`POST /firmos/voice/appointment`
```json
{
  "org_id": "...",
  "contact_id": "...",
  "summary": "Consultation — Immigration detention",
  "start_at": "2026-05-02T10:00:00Z",
  "end_at": "2026-05-02T11:00:00Z"
}
```
- POST to Clio `POST /api/v4/calendar_entries.json`
- Insert `clio_calendar_entries` row
- Returns: `{clio_entry_id, confirmed: true}`

`POST /firmos/voice/escalate`
```json
{"org_id": "...", "contact_id": "...", "reason": "caller detained by ICE"}
```
- Query `org_users WHERE org_id=? AND escalation_routing=TRUE AND status='active'` → get phone
- Async invoke `firmos-escalation` (logs audit + sends SES email to partner)
- Returns: `{transfer_to: "+19365551234"}` — ElevenLabs live-transfers the call

---

### `firmos-voice-webhook` (repurpose `firmos-vapi-webhook`)

**Trigger:** POST `/firmos/voice/webhook` — ElevenLabs post-call webhook  
**Auth:** HMAC signature verification using `firmos/voice/webhook-secret`  
**Timeout:** 30s

**What it does:**
1. Parse ElevenLabs post-call payload: `call_id`, `agent_id`, transcript turns, duration, metadata
2. Resolve org by `agent_id` → look up `organizations WHERE elevenlabs_agent_id=?`
3. Find or create contact by caller phone
4. Insert `conversations` row: `channel='voice'`, `agent_type='voice_reception'`
5. Insert each transcript turn as `messages` row: `direction='inbound'` for caller, `'outbound'` for agent
6. If `escalated=true` in metadata → invoke `firmos-escalation`
7. `log_audit` event: `voice.call_completed`

---

## Database Changes

### Migration 022

```sql
-- Rename vapi columns to elevenlabs equivalents
ALTER TABLE firm_os.organizations
    RENAME COLUMN vapi_assistant_id TO elevenlabs_agent_id;

ALTER TABLE firm_os.organizations
    ADD COLUMN IF NOT EXISTS elevenlabs_voice_id TEXT;

-- Voice secret for webhook + tool auth
-- (stored in Secrets Manager, not DB — no DB change needed)
```

No new tables required. Existing tables cover everything:
- `conversations` — `channel='voice'` already supported
- `messages` — `vapi_turn_id` column repurposed for ElevenLabs turn IDs
- `clio_calendar_entries` — used for availability + booking
- `intake_records` — same structure as SMS intake
- `audit_log` — `voice.call_completed`, `voice.escalation_triggered`

---

## API Gateway Routes

All under existing API Gateway `kezjhodcig`, new resource `/firmos/voice/`:

| Method | Route | Lambda | Auth |
|---|---|---|---|
| GET | `/firmos/voice/caller` | firmos-voice-tools | X-Voice-Secret header |
| POST | `/firmos/voice/intake` | firmos-voice-tools | X-Voice-Secret header |
| GET | `/firmos/voice/availability` | firmos-voice-tools | X-Voice-Secret header |
| POST | `/firmos/voice/appointment` | firmos-voice-tools | X-Voice-Secret header |
| POST | `/firmos/voice/escalate` | firmos-voice-tools | X-Voice-Secret header |
| POST | `/firmos/voice/webhook` | firmos-voice-webhook | ElevenLabs HMAC signature |

---

## Secrets Manager

| Secret | Format | Purpose |
|---|---|---|
| `firmos/elevenlabs/api-key` | `{"api_key": "..."}` | ElevenLabs API — provision agents, manage voices |
| `firmos/voice/webhook-secret` | `{"secret": "..."}` | HMAC verify for voice-webhook + X-Voice-Secret for voice-tools |

---

## Onboarding Addition (`firmos-org-setup`)

When a new firm is onboarded, add ElevenLabs agent provisioning step:
1. Call ElevenLabs API to create a new ConvAI agent
2. Inject firm-specific system prompt (name, practice area, hours, timezone)
3. Configure all 5 tools with correct API GW URLs + `X-Voice-Secret` header
4. Set default voice (Rachel)
5. Link agent to firm's Twilio intake number via ElevenLabs Twilio integration
6. Store `elevenlabs_agent_id` on `organizations` row

---

## Dashboard

### `/calls`
- Table: date, caller phone, caller name (if matched), duration, outcome, language
- Outcome values: `intake_completed`, `appointment_booked`, `status_check`, `escalated`, `transferred`
- Escalated rows highlighted red
- Click row → full transcript (reuse conversation detail component)
- Data source: `conversations WHERE channel='voice'` + `messages` — same `firmos-crud` Lambda

### `/settings/voice`
- Voice dropdown (fetched from ElevenLabs voices API)
- Custom greeting override text field
- After-hours message override text field
- Appointment confirmation toggle (default: off — agent books without attorney confirmation)
- Save → PATCH `organizations` + call ElevenLabs API to update agent config

---

## Secrets to Create Before Build

1. `firmos/elevenlabs/api-key` — get from ElevenLabs dashboard
2. `firmos/voice/webhook-secret` — generate random 32-byte hex string

---

## Exit Criteria

- Inbound call to Vega Law Twilio number → ElevenLabs agent answers, introduces itself with disclaimer
- New caller completes intake → `intake_records` row created, Clio contact + matter pushed
- Existing client asks about case → agent reads Clio data, responds accurately
- Caller requests appointment → agent books it in Clio, confirms date/time verbally
- Caller says emergency keyword → agent says "connecting you now" → live transfers to attorney phone
- Post-call transcript visible in `/calls` dashboard
- Bilingual: Spanish caller gets full Spanish conversation

---

## What We Are NOT Building

- Custom STT/TTS pipeline (ElevenLabs handles it)
- Voice mail / recording storage (ElevenLabs stores recordings; we store transcripts only)
- Scheduled outbound calls
- Multi-party conference calls
- SMS fallback if call drops (separate feature, Phase 6+)
