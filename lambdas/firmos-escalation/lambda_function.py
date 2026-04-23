import json
import logging
import boto3
import sys
sys.path.insert(0, '/opt/python')

from shared_db import get_connection, log_audit

logger = logging.getLogger(__name__)

def lambda_handler(event, context):
    org_id = event.get('org_id')
    if not org_id:
        return {'statusCode': 400, 'body': json.dumps({'error': 'Missing required field: org_id'})}
    contact_id = event.get('contact_id')
    if not contact_id:
        return {'statusCode': 400, 'body': json.dumps({'error': 'Missing required field: contact_id'})}
    conv_id = event.get('conversation_id')
    if not conv_id:
        return {'statusCode': 400, 'body': json.dumps({'error': 'Missing required field: conversation_id'})}
    keyword = event.get('triggered_keyword')
    if not keyword:
        return {'statusCode': 400, 'body': json.dumps({'error': 'Missing required field: triggered_keyword'})}
    message_body = event.get('message_body', '')

    conn = get_connection()

    with conn.cursor() as cur:
        cur.execute("SELECT * FROM firm_os.organizations WHERE org_id = %s", (org_id,))
        org = cur.fetchone()

    if not org:
        raise ValueError(f"org not found: {org_id}")

    with conn.cursor() as cur:
        cur.execute("SELECT phone, name FROM firm_os.contacts WHERE contact_id = %s", (contact_id,))
        contact = cur.fetchone()

    if not contact:
        raise ValueError(f"contact not found: {contact_id}")

    # Fix 1: Idempotency — skip if an open escalation already exists for this keyword+conversation
    with conn.cursor() as cur:
        cur.execute(
            "SELECT escalation_id FROM firm_os.escalations "
            "WHERE conversation_id = %s AND triggered_keyword = %s AND status = 'open'",
            (conv_id, keyword)
        )
        existing = cur.fetchone()

    if existing:
        return  # Idempotent — already escalated for this keyword on this conversation

    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO firm_os.escalations "
            "(org_id, contact_id, conversation_id, triggered_keyword, severity, status) "
            "VALUES (%s, %s, %s, %s, 'critical', 'open') RETURNING escalation_id",
            (org_id, contact_id, conv_id, keyword)
        )
        esc = cur.fetchone()
    conn.commit()

    # Fix 2: Audit immediately after commit — guaranteed even if notifications fail
    log_audit(conn, org_id, 'system', 'escalation.fired',
              {'keyword': keyword, 'contact_id': contact_id, 'esc_id': str(esc['escalation_id'])},
              'critical')

    with conn.cursor() as cur:
        cur.execute(
            "SELECT u.user_id, u.email, u.name FROM firm_os.org_users u "
            "WHERE u.org_id = %s AND u.escalation_routing = TRUE",
            (org_id,)
        )
        attorneys = cur.fetchall()

    # Fix 4: Empty attorneys guard — escalation is recorded, operator must resolve
    if not attorneys:
        logger.error(
            "escalation fired but no attorneys with escalation_routing=TRUE org_id=%s esc_id=%s",
            org_id, str(esc['escalation_id'])
        )
        return

    secrets = boto3.client('secretsmanager', region_name='us-east-2')
    secret = json.loads(secrets.get_secret_value(SecretId=org['secret_arn'])['SecretString'])
    ses_secret = json.loads(
        secrets.get_secret_value(SecretId='firmos/ses/sender')['SecretString']
    )
    ses_sender = ses_secret['sender']

    ses = boto3.client('ses', region_name='us-east-2')
    lambda_client = boto3.client('lambda', region_name='us-east-2')

    # Fix 3: Per-attorney try/except — failure for one attorney doesn't block others
    for atty in attorneys:
        try:
            subject = f"[URGENT] Escalation — {org['name']} — {contact.get('name') or contact['phone']}"
            body = (
                f"Escalation triggered by keyword: {keyword}\n"
                f"Client: {contact.get('name') or 'Unknown'} ({contact['phone']})\n"
                f"Message: {message_body}\n"
                f"Conversation ID: {conv_id}\n"
                f"Review in dashboard: https://firm-os-admin.lovable.app/escalations"
            )
            ses.send_email(
                Source=ses_sender,
                Destination={'ToAddresses': [atty['email']]},
                Message={
                    'Subject': {'Data': subject},
                    'Body': {'Text': {'Data': body}}
                }
            )

            if atty.get('phone'):
                lambda_client.invoke(
                    FunctionName='firmos-twilio-send',
                    InvocationType='Event',
                    Payload=json.dumps({
                        'org_id': org_id,
                        'to_phone': atty['phone'],
                        'body': f"[URGENT] Firm OS escalation: {contact['phone']} — keyword: {keyword}",
                        'subaccount_token': secret['twilio_auth_token']
                    }).encode()
                )
        except Exception as e:
            logger.error("escalation notify failed for atty=%s error=%s", atty.get('user_id'), type(e).__name__)
            continue  # Keep trying other attorneys
