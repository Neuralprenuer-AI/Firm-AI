# lambdas/firmos-sms-router/lambda_function.py
import json
import boto3
import sys
from datetime import datetime
from zoneinfo import ZoneInfo
sys.path.insert(0, '/opt/python')

from shared_db import get_connection, log_audit

STOP_KEYWORDS = ['stop', 'stopall', 'unsubscribe', 'cancel', 'end', 'quit']

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

def _is_after_hours(org):
    tz_name = org.get('timezone') or 'America/Chicago'
    try:
        tz = ZoneInfo(tz_name)
        now = datetime.now(tz)
        # Business hours: Mon-Fri 8am-6pm
        if now.weekday() >= 5:
            return True
        return not (8 <= now.hour < 18)
    except Exception:
        return False

def lambda_handler(event, context):
    org_id = event.get('org_id')
    if not org_id:
        return {'statusCode': 400, 'body': json.dumps({'error': 'Missing required field: org_id'})}
    from_phone = event.get('from_phone')
    if not from_phone:
        return {'statusCode': 400, 'body': json.dumps({'error': 'Missing required field: from_phone'})}
    body_raw = event.get('body')
    if body_raw is None:
        return {'statusCode': 400, 'body': json.dumps({'error': 'Missing required field: body'})}
    body = body_raw.strip()
    message_sid = event.get('message_sid', '')
    num_media = int(event.get('num_media', 0))
    conn = get_connection()

    with conn.cursor() as cur:
        cur.execute(
            "SELECT * FROM firm_os.organizations WHERE org_id = %s AND status = 'active'",
            (org_id,)
        )
        org = cur.fetchone()

    if not org:
        return

    # TCPA opt-out — must be first thing checked, before any contact lookup
    if body.lower().strip() in STOP_KEYWORDS:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE firm_os.contacts SET opted_out = TRUE WHERE org_id = %s AND phone = %s",
                (org_id, from_phone)
            )
        conn.commit()
        _send_sms(org, None, from_phone,
                  "You have been unsubscribed. No further messages will be sent. Reply START to resubscribe.")
        return

    if body.lower().strip() == 'start':
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE firm_os.contacts SET opted_out = FALSE WHERE org_id = %s AND phone = %s",
                (org_id, from_phone)
            )
        conn.commit()
        _send_sms(org, None, from_phone,
                  "You have been resubscribed. Welcome back!")
        return

    with conn.cursor() as cur:
        cur.execute(
            "SELECT * FROM firm_os.contacts WHERE org_id = %s AND phone = %s",
            (org_id, from_phone)
        )
        contact = cur.fetchone()

    if contact and contact.get('opted_out'):
        return  # Silently drop — contact opted out

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
        # Returning client who finished intake gets a status conversation, not a new intake
        with conn.cursor() as cur:
            cur.execute(
                "SELECT intake_id FROM firm_os.intake_records WHERE contact_id = %s LIMIT 1",
                (contact['contact_id'],)
            )
            has_intake = cur.fetchone()
        new_state = 'status' if has_intake else 'intake_in_progress'
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO firm_os.conversations (org_id, contact_id, state) "
                "VALUES (%s, %s, %s) RETURNING *",
                (org_id, contact['contact_id'], new_state)
            )
            conv = cur.fetchone()
        conn.commit()

    conv_id = str(conv['conversation_id'])
    state = conv['state']

    # MessageSid deduplication — Twilio may retry webhooks
    if message_sid:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT message_id FROM firm_os.messages WHERE twilio_message_sid = %s",
                (message_sid,)
            )
            if cur.fetchone():
                return  # Already processed

    # Log inbound message
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO firm_os.messages (org_id, conversation_id, direction, body, twilio_message_sid) "
            "VALUES (%s, %s, 'inbound', %s, %s)",
            (org_id, conv_id, body, message_sid or None)
        )
    conn.commit()

    # MMS — client sent a photo/document
    if num_media > 0:
        lang = contact.get('preferred_language', 'en')
        ack = ("Got it! We received your document. An attorney will review it shortly."
               if lang == 'en' else
               "¡Recibido! Hemos recibido su documento. Un abogado lo revisará pronto.")
        _send_sms(org, conv_id, from_phone, ack)
        log_audit(conn, org_id, 'system', 'sms.mms_received',
                  {'contact_id': str(contact['contact_id']), 'num_media': num_media})
        return

    # After-hours check — only for new intake messages (not escalations)
    if is_new_conv and _is_after_hours(org):
        lang = contact.get('preferred_language', 'en') if not is_new_contact else 'en'
        after_hours_msg = (
            org.get('after_hours_en') or
            "Thanks for reaching out! Our office is currently closed. We'll respond the next business day."
        ) if lang == 'en' else (
            org.get('after_hours_es') or
            "¡Gracias por contactarnos! Nuestra oficina está cerrada ahora. Le responderemos el siguiente día hábil."
        )
        _send_sms(org, conv_id, from_phone, after_hours_msg)
        return

    # Intake in progress or active — agent-core handles everything
    # (escalation decisions owned by agent-core via structured AgentResponse)
    if state in ('intake_in_progress', 'active'):
        _invoke('firmos-agent-core', {
            'org_id': org_id,
            'contact_id': str(contact['contact_id']),
            'conversation_id': conv_id,
            'user_message': body,
            'contact_phone': from_phone,
            'is_new_contact': is_new_contact,
            'current_mode': 'intake',
        })
        return

    # Escalated — agent-core answers naturally, knows attorney is already coming
    if state == 'escalated':
        _invoke('firmos-agent-core', {
            'org_id': org_id,
            'contact_id': str(contact['contact_id']),
            'conversation_id': conv_id,
            'user_message': body,
            'contact_phone': from_phone,
            'is_new_contact': False,
            'current_mode': 'emergency',
        })
        return

    # Returning client — status bot
    if state in ('complete', 'status'):
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
