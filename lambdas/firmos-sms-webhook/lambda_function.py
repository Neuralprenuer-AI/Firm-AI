import json
import urllib.parse
import boto3
import sys
sys.path.insert(0, '/opt/python')

from shared_db import get_connection
from shared_twilio import validate_signature

def lambda_handler(event, context):
    params = dict(urllib.parse.parse_qsl(event.get('body', '')))
    from_number = params.get('From', '')
    to_number = params.get('To', '')
    body = params.get('Body', '')
    signature = event.get('headers', {}).get('X-Twilio-Signature', '')
    domain = event.get('requestContext', {}).get('domainName', '')
    path = event.get('requestContext', {}).get('path', '')
    url = f"https://{domain}{path}"

    conn = get_connection()
    with conn.cursor() as cur:
        cur.execute(
            "SELECT org_id, twilio_subaccount_sid, secret_arn "
            "FROM firm_os.organizations WHERE twilio_phone_number = %s AND status = 'active'",
            (to_number,)
        )
        org = cur.fetchone()

    if not org:
        return {'statusCode': 404, 'body': ''}

    secrets = boto3.client('secretsmanager', region_name='us-east-2')
    secret = json.loads(
        secrets.get_secret_value(SecretId=org['secret_arn'])['SecretString']
    )

    try:
        validate_signature(
            auth_token=secret['twilio_auth_token'],
            signature=signature,
            url=url,
            params=params
        )
    except ValueError:
        return {'statusCode': 403, 'body': ''}

    lambda_client = boto3.client('lambda', region_name='us-east-2')
    lambda_client.invoke(
        FunctionName='firmos-sms-router',
        InvocationType='Event',
        Payload=json.dumps({
            'org_id': str(org['org_id']),
            'from_phone': from_number,
            'to_phone': to_number,
            'body': body
        }).encode()
    )

    return {
        'statusCode': 200,
        'headers': {'Content-Type': 'text/xml'},
        'body': '<?xml version="1.0" encoding="UTF-8"?><Response></Response>'
    }
