# lambdas/firmos-sms-router/lambda_function.py
import json
import boto3
import sys
sys.path.insert(0, '/opt/python')

from shared_db import get_connection, log_audit

LANGUAGE_PROMPT = "Welcome! Reply 1 for English / Responde 2 para Español."
ESCALATION_KEYWORDS_EN = ['emergency', 'urgent', 'arrested', 'injured', 'dying']
ESCALATION_KEYWORDS_ES = ['arrestado', 'detenido', 'ICE', 'herido', 'me llevaron', 'emergencia']

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

    if not contact:
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

    if not conv:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO firm_os.conversations (org_id, contact_id, state) "
                "VALUES (%s, %s, 'language_pending') RETURNING *",
                (org_id, contact['contact_id'])
            )
            conv = cur.fetchone()
        conn.commit()
        _send_sms(org, str(conv['conversation_id']), from_phone, LANGUAGE_PROMPT)
        return

    state = conv['state']
    conv_id = str(conv['conversation_id'])

    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO firm_os.messages (org_id, conversation_id, direction, body) "
            "VALUES (%s, %s, 'inbound', %s)",
            (org_id, conv_id, body)
        )
    conn.commit()

    body_lower = body.lower()
    all_keywords = ESCALATION_KEYWORDS_EN + ESCALATION_KEYWORDS_ES
    triggered = [kw for kw in all_keywords if kw.lower() in body_lower]
    if triggered:
        _invoke('firmos-escalation', {
            'org_id': org_id,
            'contact_id': str(contact['contact_id']),
            'conversation_id': conv_id,
            'triggered_keyword': triggered[0],
            'message_body': body
        })

    if state == 'language_pending':
        if body.strip() == '1':
            lang, welcome = 'en', "Great! Let's get started. I'll be collecting some information for the firm."
        elif body.strip() == '2':
            lang, welcome = 'es', "¡Perfecto! Voy a recopilar información para el despacho."
        else:
            _send_sms(org, conv_id, from_phone, LANGUAGE_PROMPT)
            return
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE firm_os.contacts SET preferred_language = %s WHERE contact_id = %s",
                (lang, contact['contact_id'])
            )
            cur.execute(
                "UPDATE firm_os.conversations SET state = 'intake_in_progress' WHERE conversation_id = %s",
                (conv_id,)
            )
        conn.commit()
        _send_sms(org, conv_id, from_phone, welcome)
        _invoke('firmos-intake-agent', {
            'org_id': org_id,
            'contact_id': str(contact['contact_id']),
            'conversation_id': conv_id,
            'language': lang,
            'message': ''
        })
        return

    if state == 'intake_in_progress':
        _invoke('firmos-intake-agent', {
            'org_id': org_id,
            'contact_id': str(contact['contact_id']),
            'conversation_id': conv_id,
            'language': contact.get('preferred_language', 'en'),
            'message': body
        })
        return

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
