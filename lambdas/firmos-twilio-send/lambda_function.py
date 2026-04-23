import json
import sys
sys.path.insert(0, '/opt/python')

from shared_db import get_connection, log_audit, assert_org_access
from shared_twilio import send_sms

def lambda_handler(event, context):
    org_id = event.get('org_id')
    if not org_id:
        return {'statusCode': 400, 'body': json.dumps({'error': 'Missing required field: org_id'})}
    to_phone = event.get('to_phone')
    if not to_phone:
        return {'statusCode': 400, 'body': json.dumps({'error': 'Missing required field: to_phone'})}
    body = event.get('body')
    if body is None:
        return {'statusCode': 400, 'body': json.dumps({'error': 'Missing required field: body'})}
    subaccount_token = event.get('subaccount_token')
    if not subaccount_token:
        return {'statusCode': 400, 'body': json.dumps({'error': 'Missing required field: subaccount_token'})}

    conn = get_connection()

    with conn.cursor() as cur:
        cur.execute(
            "SELECT org_id, twilio_phone_number, twilio_subaccount_sid, "
            "monthly_sms_budget FROM firm_os.organizations WHERE org_id = %s",
            (org_id,)
        )
        org = cur.fetchone()

    with conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) FROM firm_os.messages "
            "WHERE org_id = %s AND direction = 'outbound' "
            "AND created_at >= date_trunc('month', NOW())",
            (org_id,)
        )
        row = cur.fetchone()
        sms_count = row['count']

    if sms_count >= org['monthly_sms_budget']:
        log_audit(conn, org_id, 'system', 'sms.budget_exceeded',
                  {'to': to_phone, 'count': sms_count}, 'critical')
        return {'success': False, 'twilio_message_sid': None, 'error': 'budget_exceeded'}

    sid = send_sms(
        from_number=org['twilio_phone_number'],
        to_number=to_phone,
        body=body,
        subaccount_sid=org['twilio_subaccount_sid'],
        subaccount_token=subaccount_token
    )

    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO firm_os.messages (org_id, conversation_id, direction, body, twilio_sid) "
            "VALUES (%s, %s, 'outbound', %s, %s)",
            (org_id, event.get('conversation_id'), body, sid)
        )
    conn.commit()

    log_audit(conn, org_id, 'system', 'sms.sent', {'to': to_phone, 'sid': sid})
    return {'success': True, 'twilio_message_sid': sid, 'error': None}
