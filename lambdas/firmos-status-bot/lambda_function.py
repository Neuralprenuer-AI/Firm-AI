import json
import re
import os
import boto3
import requests
import sys
from datetime import datetime, timezone
sys.path.insert(0, '/opt/python')

from shared_db import get_connection, log_audit
from shared_ai import call_gemini, load_prompt_from_s3

CLIO_API = 'https://app.clio.com/api/v4'
REGION = os.environ.get('AWS_REGION', 'us-east-2')
SMS_CHAR_LIMIT = 320

def _invoke_send(org, conv_id, to_phone, body):
    secret = json.loads(
        boto3.client('secretsmanager', region_name=REGION)
        .get_secret_value(SecretId=org['secret_arn'])['SecretString']
    )
    boto3.client('lambda', region_name=REGION).invoke(
        FunctionName='firmos-twilio-send',
        InvocationType='Event',
        Payload=json.dumps({
            'org_id': str(org['org_id']),
            'to_phone': to_phone,
            'body': body,
            'conversation_id': conv_id,
            'subaccount_token': secret['twilio_auth_token']
        }).encode()
    )

def _split_and_send(org, conv_id, to_phone, text):
    if len(text) <= SMS_CHAR_LIMIT:
        _invoke_send(org, conv_id, to_phone, text)
        return
    sentences = re.split(r'(?<=[.!?])\s+', text.strip())
    chunk = ''
    for s in sentences:
        if len(chunk) + len(s) + 1 <= SMS_CHAR_LIMIT:
            chunk = (chunk + ' ' + s).strip() if chunk else s
        else:
            if chunk:
                _invoke_send(org, conv_id, to_phone, chunk)
            chunk = s
    if chunk:
        _invoke_send(org, conv_id, to_phone, chunk)


def _clio_token_valid(org):
    expires = org.get('clio_token_expires_at')
    if not expires:
        return False
    if isinstance(expires, str):
        try:
            expires = datetime.fromisoformat(expires.replace('Z', '+00:00'))
        except ValueError:
            return False
    return expires > datetime.now(timezone.utc)


def _get_clio_context(clio_token, clio_contact_id):
    if not clio_token or not clio_contact_id:
        return None
    try:
        resp = requests.get(
            f"{CLIO_API}/matters",
            headers={'Authorization': f'Bearer {clio_token}'},
            params={
                'contact_id': clio_contact_id,
                'status': 'open',
                'fields': 'id,display_number,description,status,practice_area,close_date,custom_field_values'
            },
            timeout=10
        )
        if resp.status_code == 200:
            matters = resp.json().get('data', [])
            if matters:
                m = matters[0]
                area = (m.get('practice_area') or {}).get('name', 'Unknown')
                desc = m.get('description') or m.get('display_number', '')
                close = m.get('close_date', '')
                return f"Matter: {area} — {desc}. Status: open.{' Close date: ' + close if close else ''}"
        return "No open matters found in case management system."
    except Exception:
        return None

def lambda_handler(event, context):
    org_id = event.get('org_id')
    contact_id = event.get('contact_id')
    conv_id = event.get('conversation_id')
    user_message = event.get('message', '')

    if not all([org_id, contact_id, conv_id]):
        return {'statusCode': 400, 'body': json.dumps({'error': 'Missing required fields'})}

    conn = get_connection()

    with conn.cursor() as cur:
        cur.execute("SELECT * FROM firm_os.organizations WHERE org_id = %s AND status = 'active'", (org_id,))
        org = cur.fetchone()
    if not org:
        return {'statusCode': 404, 'body': json.dumps({'error': f'org not found: {org_id}'})}

    with conn.cursor() as cur:
        cur.execute(
            "SELECT phone, preferred_language, name, clio_contact_id FROM firm_os.contacts "
            "WHERE contact_id = %s AND org_id = %s",
            (contact_id, org_id)
        )
        contact = cur.fetchone()
    if not contact:
        return {'statusCode': 404, 'body': json.dumps({'error': f'contact not found: {contact_id}'})}

    language = contact.get('preferred_language', 'en')
    firm_name = org.get('name', 'the firm')
    practice_area = org.get('practice_area', 'general')

    # Pull this conversation's history
    with conn.cursor() as cur:
        cur.execute(
            "SELECT direction, body FROM firm_os.messages "
            "WHERE conversation_id = %s ORDER BY created_at ASC",
            (conv_id,)
        )
        history = cur.fetchall()

    # Pull intake summary from the most recent completed intake
    with conn.cursor() as cur:
        cur.execute(
            "SELECT data, created_at FROM firm_os.intake_records "
            "WHERE contact_id = %s AND org_id = %s ORDER BY created_at DESC LIMIT 1",
            (contact_id, org_id)
        )
        intake_record = cur.fetchone()

    intake_summary = ''
    client_name = contact.get('name', '')
    if intake_record and intake_record.get('data'):
        data = intake_record['data'] if isinstance(intake_record['data'], dict) else json.loads(intake_record['data'])
        history_text = data.get('history', '')
        # Pull name from intake history if not stored on contact
        if not client_name and history_text:
            intake_summary = history_text[-800:]  # last 800 chars of intake

    # Pull Clio case context — only if token is still valid
    clio_token = org.get('clio_access_token') if _clio_token_valid(org) else None
    clio_context = _get_clio_context(clio_token, contact.get('clio_contact_id'))

    # Load status prompt (falls back to intake prompt if status_v1 not found)
    try:
        system_prompt = load_prompt_from_s3(practice_area, 'status_v1')
    except Exception:
        system_prompt = load_prompt_from_s3(practice_area)

    # Build rich context block
    context_block = f"\nFIRM: {firm_name}"
    if client_name:
        context_block += f"\nCLIENT NAME: {client_name}"
    if clio_context:
        context_block += f"\nCASE STATUS: {clio_context}"
    elif intake_summary:
        context_block += f"\nINTAKE NOTES: {intake_summary}"
    if language == 'es':
        context_block += "\nLANGUAGE: Respond in Spanish only."

    system_prompt += context_block

    # Build conversation for Gemini
    conversation_text = '\n'.join(
        f"{'Client' if m['direction'] == 'inbound' else 'Assistant'}: {m['body']}"
        for m in history
    )
    if user_message:
        conversation_text += f"\nClient: {user_message}"

    reply = call_gemini(system_prompt=system_prompt, user_message=f"{conversation_text}\nAssistant:")

    if not reply or not reply.strip():
        reply = ("Our team will be in touch shortly." if language == 'en'
                 else "Nuestro equipo se comunicará pronto.")

    # Detect callback/appointment request — log it
    callback_triggers = ['appointment', 'cita', 'llamada', 'call back', 'hablar', 'speak', 'schedule', 'reunión', 'meet']
    if any(t in user_message.lower() for t in callback_triggers):
        log_audit(conn, org_id, 'status-bot', 'callback.requested',
                  {'contact_id': contact_id, 'phone': contact['phone'], 'message': user_message})

    # Detect escalation
    escalation_triggers = ['emergency', 'emergencia', 'arrested', 'arrestado', 'ICE', 'detenido', 'urgent', 'urgente']
    if any(t.lower() in user_message.lower() for t in escalation_triggers) or 'ESCALATE' in reply:
        boto3.client('lambda', region_name=REGION).invoke(
            FunctionName='firmos-escalation',
            InvocationType='Event',
            Payload=json.dumps({
                'org_id': org_id,
                'contact_id': contact_id,
                'conversation_id': conv_id,
                'triggered_keyword': 'status_bot_escalation',
                'message_body': user_message
            }).encode()
        )
        return {'statusCode': 200, 'body': json.dumps({'status': 'escalated'})}

    # Store outbound message
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO firm_os.messages (org_id, conversation_id, direction, body) VALUES (%s, %s, 'outbound', %s)",
            (org_id, conv_id, reply)
        )
    conn.commit()

    log_audit(conn, org_id, 'status-bot', 'status.replied', {'contact_id': contact_id})
    _split_and_send(org, conv_id, contact['phone'], reply)
