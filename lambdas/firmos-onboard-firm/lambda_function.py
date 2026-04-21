# lambdas/firmos-onboard-firm/lambda_function.py
import json
import uuid
import boto3
import requests
import sys
import os
import urllib.parse
sys.path.insert(0, '/opt/python')

from shared_auth import auth_context, require_role
from shared_db import get_connection, log_audit

CLIO_AUTH_URL = 'https://app.clio.com/oauth/authorize'

def _resp(status, body):
    return {
        'statusCode': status,
        'headers': {'Content-Type': 'application/json', 'Access-Control-Allow-Origin': '*'},
        'body': json.dumps(body, default=str)
    }

def lambda_handler(event, context):
    try:
        claims = auth_context(event)
        require_role(claims, 'super_admin')
    except PermissionError as e:
        return _resp(403, {'error': str(e)})

    body = json.loads(event.get('body') or '{}')
    required = ['name', 'practice_area', 'partner_email', 'partner_name']
    missing = [f for f in required if not body.get(f)]
    if missing:
        return _resp(400, {'error': f'missing fields: {missing}'})

    conn = get_connection()
    secrets = boto3.client('secretsmanager', region_name=os.environ.get('AWS_REGION', 'us-east-2'))

    try:
        twilio_creds = json.loads(
            secrets.get_secret_value(SecretId='firmos/twilio/account-sid')['SecretString']
        )
        twilio_sid = twilio_creds['sid']
        twilio_token = json.loads(
            secrets.get_secret_value(SecretId='firmos/twilio/auth-token')['SecretString']
        )['token']
        clio_creds = json.loads(
            secrets.get_secret_value(SecretId='firmos/clio/oauth')['SecretString']
        )
    except Exception as e:
        import logging
        logging.getLogger(__name__).error("secrets fetch failed: %s", type(e).__name__)
        return _resp(500, {'error': 'secrets unavailable'})

    # Create Twilio subaccount
    try:
        sub_resp = requests.post(
            'https://api.twilio.com/2010-04-01/Accounts.json',
            auth=(twilio_sid, twilio_token),
            data={'FriendlyName': body['name']},
            timeout=15
        )
    except Exception as e:
        import logging
        logging.getLogger(__name__).error("twilio call failed: %s", type(e).__name__)
        return _resp(500, {'error': 'twilio unavailable'})
    if sub_resp.status_code != 201:
        import logging
        logging.getLogger(__name__).error("twilio subaccount failed status=%s", sub_resp.status_code)
        return _resp(500, {'error': 'twilio subaccount creation failed'})

    sub = sub_resp.json()
    sub_sid = sub['sid']
    sub_token = sub['auth_token']

    org_id = str(uuid.uuid4())
    secret_name = f"firmos/orgs/{org_id}"

    # Store subaccount credentials in Secrets Manager
    try:
        secrets.create_secret(
            Name=secret_name,
            SecretString=json.dumps({
                'twilio_auth_token': sub_token,
                'twilio_subaccount_sid': sub_sid
            })
        )
        secret_info = secrets.describe_secret(SecretId=secret_name)
        secret_arn = secret_info['ARN']
    except Exception as e:
        import logging
        logging.getLogger(__name__).error("secret create failed: %s", type(e).__name__)
        return _resp(500, {'error': 'failed to store credentials'})

    # Insert organization
    intake_extra = body.get('intake_extra', {})
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO firm_os.organizations "
                "(org_id, name, practice_area, intake_extra, city, state, billing_status, "
                "twilio_subaccount_sid, secret_arn) "
                "VALUES (%s, %s, %s, %s, %s, %s, 'trial', %s, %s) RETURNING org_id",
                (
                    org_id, body['name'], body['practice_area'],
                    json.dumps(intake_extra),
                    body.get('city'), body.get('state'),
                    sub_sid, secret_arn
                )
            )
            org_id = str(cur.fetchone()['org_id'])

        # Insert partner as firm_admin
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO firm_os.org_users "
                "(org_id, name, email, org_role, escalation_routing) "
                "VALUES (%s, %s, %s, 'partner', TRUE) RETURNING user_id",
                (org_id, body['partner_name'], body['partner_email'])
            )

        conn.commit()
    except Exception as e:
        conn.rollback()
        # Clean up the orphaned secret
        try:
            secrets.delete_secret(SecretId=secret_name, ForceDeleteWithoutRecovery=True)
        except Exception:
            pass
        import logging
        logging.getLogger(__name__).error("db insert failed, rolled back: %s org_id=%s", type(e).__name__, org_id)
        return _resp(500, {'error': 'provisioning failed, please retry'})
    log_audit(conn, org_id, claims.get('sub', 'system'), 'org.provisioned',
              {'name': body['name'], 'practice_area': body['practice_area']})

    # Build Clio OAuth URL for the dashboard to present
    clio_oauth_url = (
        f"{CLIO_AUTH_URL}?"
        + urllib.parse.urlencode({
            'response_type': 'code',
            'client_id': clio_creds['client_id'],
            'redirect_uri': clio_creds['redirect_uri'],
            'state': org_id,
            'scope': 'openid'
        })
    )

    return _resp(201, {
        'org_id': org_id,
        'twilio_subaccount_sid': sub_sid,
        'secret_arn': secret_arn,
        'clio_oauth_url': clio_oauth_url,
        'next_step': 'Configure Twilio phone number in Twilio console, then complete Clio OAuth via the provided URL'
    })
