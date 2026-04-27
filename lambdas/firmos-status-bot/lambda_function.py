import json
import re
import boto3
import sys
sys.path.insert(0, '/opt/python')

from shared_db import get_connection, log_audit
from shared_ai import call_gemini, load_prompt_from_s3

REGION = 'us-east-2'
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



def _get_clio_context(conn, org_id, contact_id):
    """Build rich context from case_status_cache + Phase 4 tables."""
    try:
        parts = []

        # Matter status from existing cache
        with conn.cursor() as cur:
            cur.execute(
                """SELECT matter_display_number, matter_status, notes_text,
                          responsible_attorney_name, open_date, close_date
                   FROM firm_os.case_status_cache
                   WHERE org_id = %s AND contact_id = %s
                   ORDER BY last_synced_at DESC NULLS LAST LIMIT 1""",
                (org_id, contact_id),
            )
            matter = cur.fetchone()
        if matter:
            matter_line = f"Matter: {matter['matter_display_number']} — Status: {matter['matter_status']}"
            if matter.get('responsible_attorney_name'):
                matter_line += f" | Attorney: {matter['responsible_attorney_name']}"
            if matter.get('open_date'):
                matter_line += f" | Opened: {matter['open_date']}"
            parts.append(matter_line)

        # Upcoming calendar entries (court dates, appointments)
        with conn.cursor() as cur:
            cur.execute(
                """SELECT summary, start_at FROM firm_os.clio_calendar_entries
                   WHERE org_id = %s AND contact_id = %s AND start_at > NOW()
                   ORDER BY start_at ASC LIMIT 3""",
                (org_id, contact_id),
            )
            entries = cur.fetchall()
        for e in entries:
            date_str = e['start_at'].strftime('%B %d, %Y') if e.get('start_at') else 'TBD'
            parts.append(f"Upcoming: {e['summary']} on {date_str}")

        # Recent notes
        with conn.cursor() as cur:
            cur.execute(
                """SELECT subject, detail FROM firm_os.clio_notes
                   WHERE org_id = %s AND contact_id = %s
                   ORDER BY synced_at DESC LIMIT 2""",
                (org_id, contact_id),
            )
            notes = cur.fetchall()
        for n in notes:
            if n.get('detail'):
                subj = (n.get('subject') or '')[:40]
                parts.append(f"Note ({subj}): {n['detail'][:200]}")

        # Recent communications
        with conn.cursor() as cur:
            cur.execute(
                """SELECT subject, body FROM firm_os.clio_communications
                   WHERE org_id = %s AND contact_id = %s
                   ORDER BY received_at DESC LIMIT 2""",
                (org_id, contact_id),
            )
            comms = cur.fetchall()
        for c in comms:
            if c.get('body'):
                subj = (c.get('subject') or '')[:40]
                parts.append(f"Comm ({subj}): {c['body'][:200]}")

        if not parts:
            return "No open matters found in case management system."
        return ' | '.join(parts)
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

    # Pull Clio case context from sync cache (no live Clio call needed)
    clio_context = _get_clio_context(conn, org_id, contact_id)

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
