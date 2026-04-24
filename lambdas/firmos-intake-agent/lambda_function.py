import json
import re
import boto3
import sys
sys.path.insert(0, '/opt/python')

from shared_db import get_connection, log_audit
from shared_ai import call_gemini, load_prompt_from_s3

SMS_CHAR_LIMIT = 320  # split above this to avoid 1600-char blobs

RE_INTAKE_TRIGGERS = [
    'new case', 'new matter', 'another case', 'different case',
    'nuevo caso', 'otro caso', 'nueva consulta'
]


def _invoke(name, payload):
    boto3.client('lambda', region_name='us-east-2').invoke(
        FunctionName=name,
        InvocationType='Event',
        Payload=json.dumps(payload).encode()
    )


def _send_sms(org, conv_id, to_phone, body):
    secrets = boto3.client('secretsmanager', region_name='us-east-2')
    secret = json.loads(secrets.get_secret_value(SecretId=org['secret_arn'])['SecretString'])
    _invoke('firmos-twilio-send', {
        'org_id': str(org['org_id']),
        'to_phone': to_phone,
        'body': body,
        'conversation_id': conv_id,
        'subaccount_token': secret['twilio_auth_token']
    })


def _split_and_send(org, conv_id, to_phone, text):
    """Split long replies at sentence boundaries before sending."""
    if len(text) <= SMS_CHAR_LIMIT:
        _send_sms(org, conv_id, to_phone, text)
        return
    sentences = re.split(r'(?<=[.!?])\s+', text.strip())
    chunk = ''
    for s in sentences:
        if len(chunk) + len(s) + 1 <= SMS_CHAR_LIMIT:
            chunk = (chunk + ' ' + s).strip() if chunk else s
        else:
            if chunk:
                _send_sms(org, conv_id, to_phone, chunk)
            chunk = s
    if chunk:
        _send_sms(org, conv_id, to_phone, chunk)


def _extract_name(conversation_text):
    """Pull a name from intake conversation if client introduced themselves."""
    patterns = [
        r"(?:my name is|i'm|i am|me llamo|soy)\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+)?)",
    ]
    for pattern in patterns:
        m = re.search(pattern, conversation_text, re.IGNORECASE)
        if m:
            return m.group(1).strip()
    return None


def lambda_handler(event, context):
    required = ['org_id', 'contact_id', 'conversation_id']
    for field in required:
        if not event.get(field):
            return {"statusCode": 400, "body": json.dumps({"error": f"Missing required field: {field}"})}

    org_id = event['org_id']
    contact_id = event['contact_id']
    conv_id = event['conversation_id']
    is_new_contact = event.get('is_new_contact', False)
    user_message = event.get('message', '')
    conversation_state = event.get('conversation_state', 'intake_in_progress')

    try:
        conn = get_connection()

        with conn.cursor() as cur:
            cur.execute("SELECT * FROM firm_os.organizations WHERE org_id = %s", (org_id,))
            org = cur.fetchone()

        if not org:
            raise ValueError(f"org_id not found: {org_id}")

        with conn.cursor() as cur:
            cur.execute(
                "SELECT direction, body FROM firm_os.messages m "
                "JOIN firm_os.conversations cv ON m.conversation_id = cv.conversation_id "
                "WHERE m.conversation_id = %s AND cv.org_id = %s ORDER BY m.created_at DESC LIMIT 15",
                (conv_id, org_id)
            )
            history = list(reversed(cur.fetchall()))

        with conn.cursor() as cur:
            cur.execute(
                "SELECT phone, name, preferred_language FROM firm_os.contacts WHERE contact_id = %s AND org_id = %s",
                (contact_id, org_id)
            )
            contact = cur.fetchone()

        language = contact.get('preferred_language') or 'en'

        # Re-intake detection: returning client wants to open a new matter
        msg_lower = user_message.lower()
        if not is_new_contact and any(t in msg_lower for t in RE_INTAKE_TRIGGERS):
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE firm_os.conversations SET state = 'complete' WHERE conversation_id = %s",
                    (conv_id,)
                )
                cur.execute(
                    "INSERT INTO firm_os.conversations (org_id, contact_id, state) VALUES (%s, %s, 'intake_in_progress') RETURNING *",
                    (org_id, contact_id)
                )
                new_conv = cur.fetchone()
            conn.commit()
            conv_id = str(new_conv['conversation_id'])
            is_new_contact = False
            history = []

        system_prompt = load_prompt_from_s3(org['practice_area'])
        if conversation_state == 'escalated':
            system_prompt += (
                "\n\nCONVERSATION STATUS: An attorney has already been notified about an urgent situation "
                "in this conversation and will contact the client soon. "
                "Do NOT escalate again. Do NOT send ESCALATE. "
                "Answer any questions the client has naturally and helpfully. "
                "If they ask about their situation, reassure them that an attorney is on the way. "
                "Keep responses brief and warm."
            )
        firm_name = org.get('name', 'the firm')
        tz = org.get('timezone') or 'America/Chicago'
        firm_context_lines = [
            f"Firm name: {firm_name}",
            f"Practice area: {org.get('practice_area', 'Law')}",
            f"Timezone: {tz}",
        ]
        if org.get('after_hours_en'):
            firm_context_lines.append(f"After-hours message (EN): {org['after_hours_en']}")
        system_prompt += "\n\nFIRM CONTEXT:\n" + "\n".join(firm_context_lines)
        if is_new_contact:
            system_prompt += (
                "\n\nNEW CLIENT: This is their very first message. "
                "Greet them warmly using the firm name. "
                "Detect their language from their message and respond in that language. "
                "Acknowledge what they said, then ask 'En qué podemos ayudarle hoy?' (or English equivalent). "
                "Do NOT ask for their name yet — let them lead."
            )
        else:
            system_prompt += "\n\nRETURNING CLIENT: They have texted before. Skip the greeting."

        if contact.get('name'):
            system_prompt += f"\n\nCLIENT NAME: {contact['name']}"

        conversation_text = '\n'.join(
            f"{'Client' if m['direction'] == 'inbound' else 'Assistant'}: {m['body']}"
            for m in history
        )
        if user_message:
            conversation_text += f"\nClient: {user_message}"

        full_prompt = f"{conversation_text}\nAssistant:"
        reply = call_gemini(system_prompt=system_prompt, user_message=full_prompt)

        # Detect and store language from Gemini's response
        if is_new_contact and reply:
            detected = 'es' if any(w in reply for w in ['¡', 'ó', 'á', 'é', 'í', 'ú', 'ñ', 'usted', 'puede']) else 'en'
            language = detected
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE firm_os.contacts SET preferred_language = %s WHERE contact_id = %s",
                    (detected, contact_id)
                )
            conn.commit()

        if not reply or not reply.strip():
            reply = ("Our team will follow up with you shortly." if language == 'en'
                     else "Nuestro equipo se comunicará con usted en breve.")

        # ABA Rule 5.3 — mandatory disclaimer on first message only
        if is_new_contact and len(history) <= 1:
            disclaimer_en = (
                org.get('mandatory_disclaimer') or
                "IMPORTANT: I'm an AI assistant, not an attorney. "
                "No attorney-client relationship is formed until confirmed in writing by a licensed attorney. "
                "Nothing I send constitutes legal advice."
            )
            disclaimer_es = (
                "IMPORTANTE: Soy un asistente de IA, no un abogado. "
                "Ninguna relación abogado-cliente se forma hasta que un abogado con licencia lo confirme por escrito. "
                "Nada de lo que envío constituye asesoría legal."
            )
            disclaimer = disclaimer_es if language == 'es' else disclaimer_en
            reply = disclaimer + "\n\n" + reply

        if 'INTAKE_COMPLETE' in reply:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT intake_id FROM firm_os.intake_records WHERE conversation_id = %s",
                    (conv_id,)
                )
                existing = cur.fetchone()
            if existing:
                return  # Idempotent

            # Try to extract client name from conversation
            extracted_name = _extract_name(conversation_text)

            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE firm_os.conversations SET state = 'complete' WHERE conversation_id = %s",
                    (conv_id,)
                )
                cur.execute(
                    "INSERT INTO firm_os.intake_records (org_id, conversation_id, contact_id, data) "
                    "VALUES (%s, %s, %s, %s) RETURNING intake_id",
                    (org_id, conv_id, contact_id, json.dumps({'history': conversation_text}))
                )
                intake = cur.fetchone()
                if extracted_name and not contact.get('name'):
                    cur.execute(
                        "UPDATE firm_os.contacts SET name = %s WHERE contact_id = %s",
                        (extracted_name, contact_id)
                    )
            conn.commit()

            closing = ("Thank you! The firm will review your information and contact you shortly."
                       if language == 'en'
                       else "¡Gracias! El despacho revisará su información y se pondrá en contacto pronto.")
            _send_sms(org, conv_id, contact['phone'], closing)

            _invoke('firmos-clio-sync', {
                'org_id': org_id,
                'contact_id': contact_id,
                'intake_id': str(intake['intake_id']),
                'conversation_text': conversation_text
            })
            log_audit(conn, org_id, 'intake-agent', 'intake.complete',
                      {'contact_id': contact_id, 'conv_id': conv_id})
            return

        if 'ESCALATE' in reply:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE firm_os.conversations SET state = 'escalated' WHERE conversation_id = %s",
                    (conv_id,)
                )
            conn.commit()
            _invoke('firmos-escalation', {
                'org_id': org_id,
                'contact_id': contact_id,
                'conversation_id': conv_id,
                'triggered_keyword': 'ai_detected',
                'message_body': user_message
            })
            holding = (
                "An attorney has been notified about your situation and will contact you shortly. "
                "If this is a life-threatening emergency, please call 911."
                if language == 'en' else
                "Un abogado ha sido notificado sobre su situación y se comunicará con usted pronto. "
                "Si es una emergencia que amenaza su vida, llame al 911."
            )
            _split_and_send(org, conv_id, contact['phone'], holding)
            return

        with conn.cursor() as cur:
            cur.execute("SELECT turn_count FROM firm_os.conversations WHERE conversation_id = %s", (conv_id,))
            tc = cur.fetchone()

        turn_count = tc['turn_count'] if tc else 0

        if turn_count >= 20:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE firm_os.conversations SET state = 'escalated' WHERE conversation_id = %s",
                    (conv_id,)
                )
            conn.commit()
            _invoke('firmos-escalation', {
                'org_id': org_id, 'contact_id': contact_id, 'conversation_id': conv_id,
                'triggered_keyword': 'turn_limit_exceeded', 'message_body': user_message
            })
            holding = ("An attorney will reach out to you directly." if language == 'en'
                       else "Un abogado se comunicará con usted directamente.")
            _split_and_send(org, conv_id, contact['phone'], holding)
            return

        # Warn client at turn 18 that they'll be connected to a person soon
        if turn_count == 18:
            warn = ("We're gathering the last few details. An attorney will reach out to you directly soon."
                    if language == 'en' else
                    "Estamos recopilando los últimos detalles. Un abogado se comunicará con usted pronto.")
            _split_and_send(org, conv_id, contact['phone'], warn)
        else:
            _split_and_send(org, conv_id, contact['phone'], reply)

        with conn.cursor() as cur:
            cur.execute(
                "UPDATE firm_os.conversations SET turn_count = turn_count + 1 WHERE conversation_id = %s",
                (conv_id,)
            )
        conn.commit()
    except Exception as e:
        import logging
        logging.getLogger(__name__).error(
            "intake-agent error: %s | org_id=%s conv_id=%s",
            type(e).__name__, org_id, event.get('conversation_id', 'unknown')
        )
        status = 400 if isinstance(e, ValueError) else 500
        return {"statusCode": status, "body": json.dumps({"error": str(e)})}
