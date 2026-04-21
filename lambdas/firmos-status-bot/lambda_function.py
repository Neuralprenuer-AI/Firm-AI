# lambdas/firmos-status-bot/lambda_function.py
import json
import os
import boto3
import requests
import sys
sys.path.insert(0, '/opt/python')

from shared_db import get_connection, log_audit

CLIO_API = 'https://app.clio.com/api/v4'

STATUS_MESSAGES = {
    'en': {
        'no_matters': "We don't have an active matter on file for you yet. A team member will follow up shortly.",
        'matters': "Your case status: {summary}. For detailed updates, please contact the firm directly.",
        'no_clio': "Our team will follow up with a case status update shortly."
    },
    'es': {
        'no_matters': "Aún no tenemos un caso activo registrado para usted. Un miembro del equipo se comunicará pronto.",
        'matters': "Estado de su caso: {summary}. Para actualizaciones detalladas, contacte directamente al despacho.",
        'no_clio': "Nuestro equipo se comunicará pronto con una actualización del estado de su caso."
    }
}

def _invoke_send(org, conv_id, to_phone, body):
    secrets = boto3.client('secretsmanager', region_name=os.environ.get('AWS_REGION', 'us-east-2'))
    secret = json.loads(secrets.get_secret_value(SecretId=org['secret_arn'])['SecretString'])
    boto3.client('lambda', region_name=os.environ.get('AWS_REGION', 'us-east-2')).invoke(
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

def lambda_handler(event, context):
    org_id = event.get('org_id')
    contact_id = event.get('contact_id')
    conv_id = event.get('conversation_id')
    language = event.get('language', 'en')

    if not all([org_id, contact_id, conv_id]):
        raise ValueError("Missing required fields: org_id, contact_id, conversation_id")

    conn = get_connection()
    msgs = STATUS_MESSAGES.get(language, STATUS_MESSAGES['en'])

    with conn.cursor() as cur:
        cur.execute("SELECT * FROM firm_os.organizations WHERE org_id = %s AND status = 'active'", (org_id,))
        org = cur.fetchone()
    if not org:
        raise ValueError(f"org not found: {org_id}")

    with conn.cursor() as cur:
        cur.execute("SELECT phone, clio_contact_id FROM firm_os.contacts WHERE contact_id = %s AND org_id = %s", (contact_id, org_id))
        contact = cur.fetchone()
    if not contact:
        raise ValueError(f"contact not found: {contact_id}")

    clio_token = org.get('clio_access_token')
    reply = msgs['no_clio']

    if clio_token and contact.get('clio_contact_id'):
        try:
            resp = requests.get(
                f"{CLIO_API}/matters",
                headers={'Authorization': f'Bearer {clio_token}'},
                params={'contact_id': contact['clio_contact_id'], 'status': 'open', 'fields': 'id,display_number,description,status,practice_area'},
                timeout=10
            )
            if resp.status_code == 200:
                matters = resp.json().get('data', [])
                if not matters:
                    reply = msgs['no_matters']
                else:
                    m = matters[0]
                    area = (m.get('practice_area', {}) or {}).get('name', '')
                    desc = m.get('description') or m.get('display_number', '')
                    summary = f"{area} — {desc}".strip(' —') if area else desc
                    reply = msgs['matters'].format(summary=summary[:100])
            else:
                reply = msgs['no_clio']
        except Exception:
            reply = msgs['no_clio']

    log_audit(conn, org_id, 'status-bot', 'status.queried', {'contact_id': contact_id})
    _invoke_send(org, conv_id, contact['phone'], reply)
