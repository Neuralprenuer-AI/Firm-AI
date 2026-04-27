import json
import re
import traceback
import logging
import boto3
import requests
import sys
from typing import Any

sys.path.insert(0, '/opt/python')
from shared_db import get_connection, log_audit

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

_VALID_PRACTICE_AREAS = frozenset({
    'immigration',
    'family_law',
    'criminal_defense',
    'personal_injury',
})

_ABA_DEFAULT_DISCLAIMER = (
    'This communication is for informational purposes only and does not constitute '
    'legal advice or create an attorney-client relationship. If you have a legal matter, '
    'please consult a licensed attorney in your jurisdiction.'
)

_SMS_WEBHOOK_URL = (
    'https://kezjhodcig.execute-api.us-east-2.amazonaws.com/prod/firmos/webhook/sms'
)

_PILOT_AREA_CODES = ('832', '713')

ELEVENLABS_API = 'https://api.elevenlabs.io/v1'
VOICE_TOOLS_BASE = 'https://kezjhodcig.execute-api.us-east-2.amazonaws.com/prod/firmos/voice'

_SYSTEM_PROMPT_TEMPLATE = """You are {agent_name}, the AI receptionist for {firm_name}, a {practice_area} law firm.

MANDATORY DISCLAIMER: At the start of every call, state: "I'm an AI assistant, not an attorney. No attorney-client relationship is formed until confirmed in writing by a licensed attorney."

YOUR ROLE:
- New callers: Collect intake information (issue, date, name, has attorney).
- Existing clients: Provide case status and upcoming appointment info.
- Appointment requests: Check availability and book.
- Emergencies or attorney requests: Transfer immediately.

HARD RULES:
- Never give legal advice, cite case law, predict outcomes, or discuss fees.
- Escalate immediately for: arrest, detention, ICE, injury, imminent court date, explicit attorney request.
- Do NOT escalate for: general questions, past events, scheduling, routine follow-up.

LANGUAGE: English by default. Switch to Spanish mid-call if caller speaks Spanish.

FIRM: {firm_name} | Practice: {practice_area} | Timezone: {timezone}
"""

_TOOLS_CONFIG = [
    {
        "name": "lookup_caller",
        "description": "Look up whether this phone number is an existing client. Returns case status and upcoming appointments.",
        "parameters": {
            "type": "object",
            "properties": {
                "phone": {"type": "string", "description": "Caller phone number in E.164 format"},
                "org_id": {"type": "string", "description": "Organization ID"},
            },
            "required": ["phone", "org_id"],
        },
        "url": f"{VOICE_TOOLS_BASE}/caller",
        "method": "GET",
    },
    {
        "name": "complete_intake",
        "description": "Record a completed intake for a new client. Call when you have collected all 4 fields: issue, incident date, name, has attorney.",
        "parameters": {
            "type": "object",
            "properties": {
                "org_id": {"type": "string"},
                "phone": {"type": "string"},
                "name": {"type": "string"},
                "issue": {"type": "string"},
                "incident_date": {"type": "string"},
                "has_attorney": {"type": "boolean"},
                "language": {"type": "string", "enum": ["en", "es"]},
            },
            "required": ["org_id", "phone", "name", "issue"],
        },
        "url": f"{VOICE_TOOLS_BASE}/intake",
        "method": "POST",
    },
    {
        "name": "check_availability",
        "description": "Check what appointment slots are available on a given date.",
        "parameters": {
            "type": "object",
            "properties": {
                "org_id": {"type": "string"},
                "date": {"type": "string", "description": "Date in YYYY-MM-DD format"},
            },
            "required": ["org_id", "date"],
        },
        "url": f"{VOICE_TOOLS_BASE}/availability",
        "method": "GET",
    },
    {
        "name": "book_appointment",
        "description": "Book an appointment for a client in the firm's calendar.",
        "parameters": {
            "type": "object",
            "properties": {
                "org_id": {"type": "string"},
                "contact_id": {"type": "string"},
                "summary": {"type": "string"},
                "start_at": {"type": "string", "description": "ISO-8601 UTC datetime"},
                "end_at": {"type": "string", "description": "ISO-8601 UTC datetime"},
            },
            "required": ["org_id", "contact_id", "summary", "start_at", "end_at"],
        },
        "url": f"{VOICE_TOOLS_BASE}/appointment",
        "method": "POST",
    },
    {
        "name": "escalate_transfer",
        "description": "Get the on-call attorney phone number for live transfer. Use for emergencies or when caller explicitly asks for an attorney.",
        "parameters": {
            "type": "object",
            "properties": {
                "org_id": {"type": "string"},
                "contact_id": {"type": "string"},
                "reason": {"type": "string"},
            },
            "required": ["org_id", "reason"],
        },
        "url": f"{VOICE_TOOLS_BASE}/escalate",
        "method": "POST",
    },
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _resp(status: int, body: dict[str, Any]) -> dict[str, Any]:
    return {
        'statusCode': status,
        'headers': {
            'Content-Type': 'application/json',
            'Access-Control-Allow-Origin': '*',
        },
        'body': json.dumps(body, default=str),
    }


def _get_secret(sm_client: Any, secret_id: str) -> dict[str, Any]:
    raw = sm_client.get_secret_value(SecretId=secret_id)['SecretString']
    return json.loads(raw)


def _mark_failed(conn: Any, org_id: str | None) -> None:
    """Best-effort: set org status to failed_setup so the dashboard can show it."""
    if org_id is None:
        return
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE firm_os.organizations SET status = 'failed_setup' WHERE org_id = %s",
                (org_id,),
            )
        conn.commit()
    except Exception:
        logger.exception("Could not mark org %s as failed_setup", org_id)


def _fail(conn: Any, org_id: str | None, step: str, message: str) -> dict[str, Any]:
    logger.error("Setup failed at step=%s org_id=%s message=%s", step, org_id, message)
    _mark_failed(conn, org_id)
    return _resp(500, {
        'error': 'setup_failed',
        'step': step,
        'message': message,
        'org_id': org_id,
    })


# ---------------------------------------------------------------------------
# Validation (Step 1)
# ---------------------------------------------------------------------------

def _validate(event: dict[str, Any]) -> dict[str, Any] | None:
    """
    Returns a 400 response dict if validation fails, else None.
    """
    if not event.get('is_super_admin'):
        return _resp(403, {'error': 'forbidden', 'message': 'super_admin required'})

    required_fields = [
        'firm_name', 'practice_area', 'timezone', 'partner_name',
        'partner_email', 'emergency_contact_number', 'agent_display_name',
        'monthly_sms_budget', 'monthly_token_budget',
    ]
    for field in required_fields:
        if event.get(field) is None or event.get(field) == '':
            return _resp(400, {
                'error': 'validation',
                'field': field,
                'message': f'{field} is required',
            })

    practice_area: str = event['practice_area']
    if practice_area not in _VALID_PRACTICE_AREAS:
        return _resp(400, {
            'error': 'validation',
            'field': 'practice_area',
            'message': (
                f"practice_area must be one of: "
                f"{', '.join(sorted(_VALID_PRACTICE_AREAS))}"
            ),
        })

    email: str = event['partner_email']
    at_pos = email.find('@')
    if at_pos < 1 or '.' not in email[at_pos + 1:]:
        return _resp(400, {
            'error': 'validation',
            'field': 'partner_email',
            'message': 'Invalid email address',
        })

    phone: str = event['emergency_contact_number']
    if not phone.startswith('+1') or len(phone) != 12 or not phone[1:].isdigit():
        return _resp(400, {
            'error': 'validation',
            'field': 'emergency_contact_number',
            'message': 'Phone must be E.164 US format: +1XXXXXXXXXX (12 chars)',
        })

    return None


# ---------------------------------------------------------------------------
# Step 2: Create org row
# ---------------------------------------------------------------------------

def _create_org(conn: Any, event: dict[str, Any]) -> str:
    disclaimer = event.get('mandatory_disclaimer') or _ABA_DEFAULT_DISCLAIMER
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO firm_os.organizations (
                name, status, billing_status, practice_area, agent_display_name,
                timezone, partner_email, emergency_contact_number,
                mandatory_disclaimer, monthly_sms_budget, monthly_token_budget
            ) VALUES (%s, 'onboarding', 'pending', %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING org_id
            """,
            (
                event['firm_name'],
                event['practice_area'],
                event['agent_display_name'],
                event['timezone'],
                event['partner_email'],
                event['emergency_contact_number'],
                disclaimer,
                int(event['monthly_sms_budget']),
                int(event['monthly_token_budget']),
            ),
        )
        row = cur.fetchone()
    conn.commit()
    return str(row['org_id'])


# ---------------------------------------------------------------------------
# Step 3: Twilio subaccount + Secrets Manager
# ---------------------------------------------------------------------------

def _provision_twilio_subaccount(
    sm_client: Any,
    org_id: str,
    firm_name: str,
    master_sid: str,
    master_token: str,
) -> tuple[str, str, str]:
    """
    Creates a Twilio subaccount, stores creds in SM.
    Returns (subaccount_sid, subaccount_auth_token, secret_arn).
    """
    resp = requests.post(
        'https://api.twilio.com/2010-04-01/Accounts.json',
        auth=(master_sid, master_token),
        data={'FriendlyName': f'{firm_name} AI Intake'},
        timeout=20,
    )
    if resp.status_code not in (200, 201):
        raise RuntimeError(f'Twilio returned {resp.status_code}: {resp.text[:400]}')

    sub = resp.json()
    sub_sid: str = sub['sid']
    sub_token: str = sub['auth_token']

    secret_name = f'firmos/orgs/{org_id}'
    secret_value = json.dumps({
        'twilio_account_sid': sub_sid,
        'twilio_auth_token': sub_token,
        'twilio_subaccount_sid': sub_sid,
    })

    create_resp = sm_client.create_secret(
        Name=secret_name,
        Description=f'Twilio credentials for Firm OS org {org_id}',
        SecretString=secret_value,
        Tags=[
            {'Key': 'Project', 'Value': 'FirmOS'},
            {'Key': 'org_id', 'Value': org_id},
        ],
    )
    secret_arn: str = create_resp['ARN']
    return sub_sid, sub_token, secret_arn


def _store_secret_arn(conn: Any, org_id: str, secret_arn: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            'UPDATE firm_os.organizations SET secret_arn = %s WHERE org_id = %s',
            (secret_arn, org_id),
        )
    conn.commit()


# ---------------------------------------------------------------------------
# Step 4: Purchase Twilio phone number
# ---------------------------------------------------------------------------

def _purchase_phone_number(
    sub_sid: str,
    sub_token: str,
) -> str:
    """
    Searches for an available US local number, preferring area codes 832 then 713.
    Falls back to any US local if neither pilot area code has availability.
    Returns the purchased E.164 phone number.
    """
    auth = (sub_sid, sub_token)
    search_base = (
        f'https://api.twilio.com/2010-04-01/Accounts/{sub_sid}'
        '/AvailablePhoneNumbers/US/Local.json'
    )

    chosen_number: str | None = None

    # Try pilot area codes first, then unrestricted
    area_code_attempts: list[str | None] = [*_PILOT_AREA_CODES, None]
    for area_code in area_code_attempts:
        params: dict[str, Any] = {'SmsEnabled': 'True', 'Limit': '5'}
        if area_code:
            params['AreaCode'] = area_code

        search_resp = requests.get(search_base, auth=auth, params=params, timeout=20)
        if search_resp.status_code != 200:
            logger.warning(
                "AvailablePhoneNumbers search returned %s for area_code=%s",
                search_resp.status_code,
                area_code,
            )
            continue

        numbers = search_resp.json().get('available_phone_numbers', [])
        if numbers:
            chosen_number = numbers[0]['phone_number']
            logger.info("Selected number %s (area_code=%s)", chosen_number, area_code)
            break

    if not chosen_number:
        raise RuntimeError(
            'No available US local SMS numbers found in any area code'
        )

    purchase_resp = requests.post(
        f'https://api.twilio.com/2010-04-01/Accounts/{sub_sid}/IncomingPhoneNumbers.json',
        auth=auth,
        data={
            'PhoneNumber': chosen_number,
            'SmsUrl': _SMS_WEBHOOK_URL,
            'SmsMethod': 'POST',
        },
        timeout=20,
    )
    if purchase_resp.status_code not in (200, 201):
        raise RuntimeError(
            f'Phone number purchase returned {purchase_resp.status_code}: '
            f'{purchase_resp.text[:400]}'
        )

    purchased: str = purchase_resp.json()['phone_number']
    return purchased


def _store_phone_number(conn: Any, org_id: str, phone_number: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            'UPDATE firm_os.organizations SET twilio_phone_number = %s WHERE org_id = %s',
            (phone_number, org_id),
        )
    conn.commit()


# ---------------------------------------------------------------------------
# Step 5: Create org_users row
# ---------------------------------------------------------------------------

def _create_org_user(conn: Any, org_id: str, event: dict[str, Any]) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO firm_os.org_users
                (org_id, email, full_name, org_role, escalation_routing, status)
            VALUES (%s, %s, %s, 'managing_partner', TRUE, 'active')
            """,
            (org_id, event['partner_email'], event['partner_name']),
        )
    conn.commit()


# ---------------------------------------------------------------------------
# Step 6: Activate org
# ---------------------------------------------------------------------------

def _activate_org(conn: Any, org_id: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE firm_os.organizations SET status = 'active' WHERE org_id = %s",
            (org_id,),
        )
    conn.commit()


# ---------------------------------------------------------------------------
# Step 7: Provision ElevenLabs ConvAI agent (non-fatal)
# ---------------------------------------------------------------------------

def _provision_elevenlabs_agent(
    sm_client: Any,
    org_id: str,
    event: dict[str, Any],
    twilio_phone_number: str,
    sub_sid: str,
    sub_token: str,
) -> 'str | None':
    """
    Creates an ElevenLabs ConvAI agent for this firm.
    Returns agent_id or None if provisioning fails (non-fatal).
    """
    try:
        el_secret = json.loads(
            sm_client.get_secret_value(SecretId='firmos/elevenlabs/api-key')['SecretString']
        )
        api_key = el_secret['api_key']
    except Exception as exc:
        logger.warning("Could not load ElevenLabs API key — skipping voice: %s", exc)
        return None

    try:
        voice_secret = json.loads(
            sm_client.get_secret_value(SecretId='firmos/voice/webhook-secret')['SecretString']
        )
        webhook_secret = voice_secret['secret']
    except Exception as exc:
        logger.warning("Could not load voice webhook secret — skipping voice: %s", exc)
        return None

    if not webhook_secret:
        logger.warning("Voice webhook secret is empty — skipping voice")
        return None

    firm_name = event['firm_name']
    practice_area = event['practice_area'].replace('_', ' ').title()
    agent_name = event.get('agent_display_name', 'Alex')
    timezone = event.get('timezone', 'America/Chicago')

    system_prompt = _SYSTEM_PROMPT_TEMPLATE.format(
        agent_name=agent_name,
        firm_name=firm_name,
        practice_area=practice_area,
        timezone=timezone,
    )

    tools_with_auth = []
    for tool in _TOOLS_CONFIG:
        tools_with_auth.append({
            **tool,
            "headers": {"X-Voice-Secret": webhook_secret},
        })

    agent_payload = {
        "name": f"{firm_name} Receptionist",
        "conversation_config": {
            "agent": {
                "prompt": {
                    "prompt": system_prompt,
                    "tools": tools_with_auth,
                },
                "first_message": (
                    f"Thank you for calling {firm_name}. I'm {agent_name}, the AI receptionist. "
                    "I'm not an attorney — no attorney-client relationship is formed until confirmed "
                    "in writing by a licensed attorney. How can I help you today?"
                ),
                "language": "en",
            },
            "tts": {"voice_id": "21m00Tcm4TlvDq8ikWAM"},
        },
        "platform_settings": {
            "webhook": {
                "url": "https://kezjhodcig.execute-api.us-east-2.amazonaws.com/prod/firmos/voice/webhook",
            }
        },
    }

    try:
        create_resp = requests.post(
            f'{ELEVENLABS_API}/conversational_ai/agents',
            headers={'xi-api-key': api_key, 'Content-Type': 'application/json'},
            json=agent_payload,
            timeout=20,
        )
    except Exception as exc:
        logger.warning("ElevenLabs agent create network error — skipping voice: %s", exc)
        return None

    if create_resp.status_code not in (200, 201):
        logger.warning("ElevenLabs agent create failed status=%s", create_resp.status_code)
        return None

    agent_id = create_resp.json().get('agent_id')
    if not agent_id:
        logger.warning("ElevenLabs agent_id missing from response")
        return None

    # Link Twilio phone number to this agent in ElevenLabs
    try:
        phone_resp = requests.post(
            f'{ELEVENLABS_API}/conversational_ai/phone_numbers',
            headers={'xi-api-key': api_key, 'Content-Type': 'application/json'},
            json={
                'phone_number': twilio_phone_number,
                'label': f'{firm_name} intake',
                'sid': sub_sid,
                'token': sub_token,
            },
            timeout=20,
        )
        if phone_resp.status_code not in (200, 201):
            logger.warning("ElevenLabs phone import failed status=%s", phone_resp.status_code)
    except Exception as exc:
        logger.warning("ElevenLabs phone import exception: %s", exc)

    logger.info("ElevenLabs agent provisioned agent_id=%s for org=%s", agent_id, org_id)
    return agent_id


# ---------------------------------------------------------------------------
# Step 8: Send welcome email via SES (non-fatal)
# ---------------------------------------------------------------------------

def _send_welcome_email(
    ses_client: Any,
    sender_address: str,
    event: dict[str, Any],
    twilio_phone_number: str,
) -> None:
    firm_name: str = event['firm_name']
    partner_name: str = event['partner_name']
    partner_email: str = event['partner_email']
    practice_area: str = event['practice_area'].replace('_', ' ').title()

    html_body = f"""<!DOCTYPE html><html><body style="font-family: sans-serif; max-width: 600px; margin: 0 auto;">
<h1 style="color: #1a1a2e;">Welcome to Firm OS</h1>
<p>Hi {partner_name},</p>
<p>Your firm <strong>{firm_name}</strong> has been onboarded to Firm OS.</p>
<p><strong>Your intake SMS number:</strong> {twilio_phone_number}</p>
<p><strong>Practice area:</strong> {practice_area}</p>
<p>Log into the dashboard to configure your settings and review incoming leads.</p>
<p style="color: #666; font-size: 12px; margin-top: 32px;">Firm OS &middot; Neuralpreneur</p>
</body></html>"""

    ses_client.send_email(
        Source=sender_address,
        Destination={'ToAddresses': [partner_email]},
        Message={
            'Subject': {
                'Data': f'Your Firm OS account is ready \u2014 {firm_name}',
                'Charset': 'UTF-8',
            },
            'Body': {
                'Html': {
                    'Data': html_body,
                    'Charset': 'UTF-8',
                },
            },
        },
    )
    logger.info("Welcome email sent to %s", partner_email)


# ---------------------------------------------------------------------------
# Lambda handler
# ---------------------------------------------------------------------------

def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    org_id: str | None = None
    conn = None

    try:
        # ------------------------------------------------------------------
        # Step 1: Validate inputs
        # ------------------------------------------------------------------
        validation_error = _validate(event)
        if validation_error is not None:
            return validation_error

        # ------------------------------------------------------------------
        # Initialise shared resources
        # ------------------------------------------------------------------
        region = 'us-east-2'
        sm_client = boto3.client('secretsmanager', region_name=region)
        ses_client = boto3.client('ses', region_name=region)
        conn = get_connection()

        # Fetch Twilio master credentials up front so a secrets failure is
        # surfaced before we touch the database.
        try:
            twilio_master = _get_secret(sm_client, 'firmos/twilio/master')
            master_sid: str = twilio_master['account_sid']
            master_token: str = twilio_master['auth_token']
        except Exception as exc:
            logger.exception("Failed to retrieve Twilio master credentials")
            return _resp(500, {
                'error': 'setup_failed',
                'step': 'secrets_fetch',
                'message': f'Could not retrieve Twilio master credentials: {exc}',
                'org_id': None,
            })

        try:
            ses_secret = _get_secret(sm_client, 'firmos/ses/sender')
            sender_address: str = ses_secret['address']
        except Exception:
            logger.exception("Failed to retrieve SES sender address — will skip email")
            sender_address = ''

        # ------------------------------------------------------------------
        # Step 2: Create org row
        # ------------------------------------------------------------------
        try:
            org_id = _create_org(conn, event)
            logger.info("Created org row org_id=%s", org_id)
        except Exception as exc:
            logger.exception("Step 2 (create_org) failed")
            return _fail(conn, None, 'create_org', str(exc))

        # ------------------------------------------------------------------
        # Step 3: Provision Twilio subaccount + store credentials
        # ------------------------------------------------------------------
        try:
            sub_sid, sub_token, secret_arn = _provision_twilio_subaccount(
                sm_client, org_id, event['firm_name'], master_sid, master_token
            )
            _store_secret_arn(conn, org_id, secret_arn)
            logger.info(
                "Twilio subaccount provisioned sub_sid=%s secret_arn=%s",
                sub_sid, secret_arn,
            )
        except Exception as exc:
            logger.exception("Step 3 (twilio_subaccount) failed org_id=%s", org_id)
            return _fail(conn, org_id, 'twilio_subaccount', str(exc))

        # ------------------------------------------------------------------
        # Step 4: Purchase Twilio phone number
        # ------------------------------------------------------------------
        try:
            twilio_phone_number = _purchase_phone_number(sub_sid, sub_token)
            _store_phone_number(conn, org_id, twilio_phone_number)
            logger.info(
                "Phone number purchased %s for org_id=%s", twilio_phone_number, org_id
            )
        except Exception as exc:
            logger.exception("Step 4 (purchase_phone_number) failed org_id=%s", org_id)
            return _fail(conn, org_id, 'purchase_phone_number', str(exc))

        # ------------------------------------------------------------------
        # Step 5: Create org_users row for managing partner
        # ------------------------------------------------------------------
        try:
            _create_org_user(conn, org_id, event)
            logger.info("org_users row created for %s org_id=%s", event['partner_email'], org_id)
        except Exception as exc:
            logger.exception("Step 5 (create_org_user) failed org_id=%s", org_id)
            return _fail(conn, org_id, 'create_org_user', str(exc))

        # ------------------------------------------------------------------
        # Step 6: Activate org
        # ------------------------------------------------------------------
        try:
            _activate_org(conn, org_id)
            logger.info("Org activated org_id=%s", org_id)
        except Exception as exc:
            logger.exception("Step 6 (activate_org) failed org_id=%s", org_id)
            return _fail(conn, org_id, 'activate_org', str(exc))

        # ------------------------------------------------------------------
        # Step 7: Provision ElevenLabs voice agent (non-fatal)
        # ------------------------------------------------------------------
        agent_id = _provision_elevenlabs_agent(
            sm_client, org_id, event, twilio_phone_number, sub_sid, sub_token
        )
        if agent_id:
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        "UPDATE firm_os.organizations SET elevenlabs_agent_id = %s WHERE org_id = %s",
                        (agent_id, org_id),
                    )
                conn.commit()
                logger.info("elevenlabs_agent_id stored org=%s agent=%s", org_id, agent_id)
            except Exception as exc:
                logger.warning("Failed to store agent_id org=%s: %s", org_id, exc)
        else:
            logger.warning("Voice not provisioned for org=%s — manual setup required", org_id)

        # ------------------------------------------------------------------
        # Step 8: Send welcome email (non-fatal)
        # ------------------------------------------------------------------
        email_sent = False
        if sender_address:
            try:
                _send_welcome_email(ses_client, sender_address, event, twilio_phone_number)
                email_sent = True
            except Exception:
                logger.warning(
                    "Step 8 (welcome_email) failed — continuing. org_id=%s partner=%s",
                    org_id,
                    event['partner_email'],
                    exc_info=True,
                )
        else:
            logger.warning(
                "Skipping welcome email: SES sender address unavailable org_id=%s", org_id
            )

        # ------------------------------------------------------------------
        # Step 9: Audit log (best effort)
        # ------------------------------------------------------------------
        try:
            log_audit(
                conn,
                org_id,
                'system',
                'org.onboarded',
                {
                    'firm_name': event['firm_name'],
                    'practice_area': event['practice_area'],
                    'partner_email': event['partner_email'],
                    'twilio_number': twilio_phone_number,
                    'created_by': 'super_admin',
                },
            )
        except Exception:
            logger.warning("Step 9 (audit_log) failed — non-fatal org_id=%s", org_id, exc_info=True)

        # ------------------------------------------------------------------
        # Success
        # ------------------------------------------------------------------
        email_note = (
            f'Welcome email sent to {event["partner_email"]}.'
            if email_sent
            else 'Welcome email could not be sent — check SES logs.'
        )
        return _resp(200, {
            'org_id': org_id,
            'firm_name': event['firm_name'],
            'twilio_phone_number': twilio_phone_number,
            'elevenlabs_agent_id': agent_id,
            'status': 'active',
            'message': f'Firm onboarded successfully. {email_note}',
        })

    except Exception:
        # Catch-all: log full traceback and return structured 500
        tb = traceback.format_exc()
        logger.error("Unhandled exception in firmos-org-setup org_id=%s\n%s", org_id, tb)
        if conn is not None:
            _mark_failed(conn, org_id)
        return _resp(500, {
            'error': 'setup_failed',
            'step': 'unknown',
            'message': 'An unexpected error occurred. See CloudWatch logs for details.',
            'org_id': org_id,
        })
