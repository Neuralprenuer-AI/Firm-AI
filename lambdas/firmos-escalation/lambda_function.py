import json
import boto3
import sys
sys.path.insert(0, '/opt/python')

from shared_db import get_connection, log_audit

def lambda_handler(event, context):
    org_id = event['org_id']
    contact_id = event['contact_id']
    conv_id = event['conversation_id']
    keyword = event['triggered_keyword']
    message_body = event.get('message_body', '')

    conn = get_connection()

    with conn.cursor() as cur:
        cur.execute("SELECT * FROM firm_os.organizations WHERE org_id = %s", (org_id,))
        org = cur.fetchone()

    with conn.cursor() as cur:
        cur.execute("SELECT phone, name FROM firm_os.contacts WHERE contact_id = %s", (contact_id,))
        contact = cur.fetchone()

    with conn.cursor() as cur:
        cur.execute(
            "SELECT u.user_id, u.email, u.name, u.phone FROM firm_os.org_users u "
            "WHERE u.org_id = %s AND u.escalation_routing = TRUE",
            (org_id,)
        )
        attorneys = cur.fetchall()

    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO firm_os.escalations "
            "(org_id, contact_id, conversation_id, triggered_keyword, severity, status) "
            "VALUES (%s, %s, %s, %s, 'critical', 'open') RETURNING escalation_id",
            (org_id, contact_id, conv_id, keyword)
        )
        esc = cur.fetchone()
    conn.commit()

    secrets = boto3.client('secretsmanager', region_name='us-east-2')
    secret = json.loads(secrets.get_secret_value(SecretId=org['secret_arn'])['SecretString'])
    ses_secret = json.loads(
        secrets.get_secret_value(SecretId='firmos/ses/sender')['SecretString']
    )
    ses_sender = ses_secret['sender']

    ses = boto3.client('ses', region_name='us-east-2')
    lambda_client = boto3.client('lambda', region_name='us-east-2')

    for atty in attorneys:
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

    log_audit(conn, org_id, 'system', 'escalation.fired',
              {'keyword': keyword, 'contact_id': contact_id, 'esc_id': str(esc['escalation_id'])},
              'critical')
