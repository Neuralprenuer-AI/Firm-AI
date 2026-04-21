import json
import boto3
import sys
sys.path.insert(0, '/opt/python')

from shared_db import get_connection, log_audit
from shared_ai import call_gemini, load_prompt_from_s3

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

def lambda_handler(event, context):
    org_id = event['org_id']
    contact_id = event['contact_id']
    conv_id = event['conversation_id']
    language = event.get('language', 'en')
    user_message = event.get('message', '')

    conn = get_connection()

    with conn.cursor() as cur:
        cur.execute("SELECT * FROM firm_os.organizations WHERE org_id = %s", (org_id,))
        org = cur.fetchone()

    with conn.cursor() as cur:
        cur.execute(
            "SELECT direction, body FROM firm_os.messages "
            "WHERE conversation_id = %s ORDER BY created_at ASC",
            (conv_id,)
        )
        history = cur.fetchall()

    with conn.cursor() as cur:
        cur.execute("SELECT phone FROM firm_os.contacts WHERE contact_id = %s", (contact_id,))
        contact = cur.fetchone()

    system_prompt = load_prompt_from_s3(org['practice_area'])
    if language == 'es':
        system_prompt += "\n\nIMPORTANT: Respond only in Spanish."

    conversation_text = '\n'.join(
        f"{'Client' if m['direction'] == 'inbound' else 'Assistant'}: {m['body']}"
        for m in history
    )
    if user_message:
        conversation_text += f"\nClient: {user_message}"

    full_prompt = f"{conversation_text}\nAssistant:"
    reply = call_gemini(system_prompt=system_prompt, user_message=full_prompt)

    if 'INTAKE_COMPLETE' in reply:
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
        _invoke('firmos-escalation', {
            'org_id': org_id,
            'contact_id': contact_id,
            'conversation_id': conv_id,
            'triggered_keyword': 'ai_detected',
            'message_body': user_message
        })
        return

    _send_sms(org, conv_id, contact['phone'], reply)
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE firm_os.conversations SET turn_count = turn_count + 1 WHERE conversation_id = %s",
            (conv_id,)
        )
    conn.commit()
