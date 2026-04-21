# lambdas/firmos-sms-router/lambda_function.py
import json
import boto3
import sys
sys.path.insert(0, '/opt/python')

from shared_db import get_connection, log_audit

ESCALATION_KEYWORDS = [
    'emergency', 'urgent', 'arrested', 'injured', 'dying',
    'arrestado', 'detenido', 'ICE', 'herido', 'me llevaron', 'emergencia'
]

def _invoke(name: str, payload: dict):
    boto3.client('lambda', region_name='us-east-2').invoke(
        FunctionName=name,
        InvocationType='Event',
        Payload=json.dumps(payload).encode()
    )

def _send_sms(org, conversation_id, to_phone, body):
    secrets = boto3.client('secretsmanager', region_name='us-east-2')
    secret = json.loads(secrets.get_secret_value(SecretId=org['secret_arn'])['SecretString'])
    _invoke('firmos-twilio-send', {
        'org_id': str(org['org_id']),
        'to_phone': to_phone,
        'body': body,
        'conversation_id': conversation_id,
        'subaccount_token': secret['twilio_auth_token']
    })

def lambda_handler(event, context):
    org_id = event['org_id']
    from_phone = event['from_phone']
    body = event['body'].strip()
    conn = get_connection()

    with conn.cursor() as cur:
        cur.execute(
            "SELECT * FROM firm_os.organizations WHERE org_id = %s AND status = 'active'",
            (org_id,)
        )
        org = cur.fetchone()

    if not org:
        return

    with conn.cursor() as cur:
        cur.execute(
            "SELECT * FROM firm_os.contacts WHERE org_id = %s AND phone = %s",
            (org_id, from_phone)
        )
        contact = cur.fetchone()

    is_new_contact = contact is None
    if is_new_contact:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO firm_os.contacts (org_id, phone, preferred_language, intake_status) "
                "VALUES (%s, %s, 'pending', 'new') RETURNING *",
                (org_id, from_phone)
            )
            contact = cur.fetchone()
        conn.commit()

    with conn.cursor() as cur:
        cur.execute(
            "SELECT * FROM firm_os.conversations "
            "WHERE org_id = %s AND contact_id = %s AND state != 'complete' "
            "ORDER BY created_at DESC LIMIT 1",
            (org_id, contact['contact_id'])
        )
        conv = cur.fetchone()

    is_new_conv = conv is None
    if is_new_conv:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO firm_os.conversations (org_id, contact_id, state) "
                "VALUES (%s, %s, 'intake_in_progress') RETURNING *",
                (org_id, contact['contact_id'])
            )
            conv = cur.fetchone()
        conn.commit()

    conv_id = str(conv['conversation_id'])
    state = conv['state']

    # Log inbound message
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO firm_os.messages (org_id, conversation_id, direction, body) "
            "VALUES (%s, %s, 'inbound', %s)",
            (org_id, conv_id, body)
        )
    conn.commit()

    # Escalation keyword check — fires for any state
    body_lower = body.lower()
    triggered = [kw for kw in ESCALATION_KEYWORDS if kw.lower() in body_lower]
    if triggered:
        _invoke('firmos-escalation', {
            'org_id': org_id,
            'contact_id': str(contact['contact_id']),
            'conversation_id': conv_id,
            'triggered_keyword': triggered[0],
            'message_body': body
        })

    # Intake in progress — Gemini handles everything
    if state == 'intake_in_progress':
        _invoke('firmos-intake-agent', {
            'org_id': org_id,
            'contact_id': str(contact['contact_id']),
            'conversation_id': conv_id,
            'is_new_contact': is_new_contact,
            'message': body
        })
        return

    # Returning client (intake complete) — status bot
    if state == 'complete':
        _invoke('firmos-status-bot', {
            'org_id': org_id,
            'contact_id': str(contact['contact_id']),
            'conversation_id': conv_id,
            'language': contact.get('preferred_language', 'en'),
            'message': body
        })
        return

    log_audit(conn, org_id, 'system', 'sms.unhandled_state',
              {'state': state, 'from': from_phone}, 'warning')
