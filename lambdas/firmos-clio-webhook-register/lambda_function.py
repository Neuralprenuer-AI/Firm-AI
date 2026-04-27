import json
import boto3
import requests
import sys
import logging
from datetime import datetime, timezone, timedelta

sys.path.insert(0, '/opt/python')
from shared_db import get_connection, log_audit

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

CLIO_API = 'https://app.clio.com/api/v4'
REGION = 'us-east-2'
WEBHOOK_URL = 'https://kezjhodcig.execute-api.us-east-2.amazonaws.com/prod/firmos/clio/webhook'
WEBHOOK_MODELS = ['matter', 'contact', 'calendar_entry', 'communication', 'note']
ORG_ID = 'a1b2c3d4-0002-0002-0002-000000000002'


def lambda_handler(event, context):
    conn = get_connection()

    with conn.cursor() as cur:
        cur.execute(
            "SELECT clio_access_token FROM firm_os.organizations WHERE org_id = %s AND status = 'active'",
            (ORG_ID,)
        )
        org = cur.fetchone()

    if not org or not org.get('clio_access_token'):
        return {'error': 'No valid token for Vega Law'}

    token = org['clio_access_token']
    headers = {'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'}
    expires_at = (datetime.now(timezone.utc) + timedelta(days=30)).strftime('%Y-%m-%dT%H:%M:%SZ')

    registered = []
    for model in WEBHOOK_MODELS:
        try:
            resp = requests.post(
                f"{CLIO_API}/webhooks.json",
                headers=headers,
                json={'data': {
                    'url': WEBHOOK_URL,
                    'model': model,
                    'events': ['created', 'updated', 'deleted'],
                    'expires_at': expires_at,
                }},
                timeout=10,
            )
            if resp.status_code in (200, 201):
                data = resp.json()['data']
                webhook_id = str(data['id'])
                with conn.cursor() as cur:
                    cur.execute(
                        """INSERT INTO firm_os.clio_webhook_subscriptions
                               (org_id, clio_webhook_id, model, url, expires_at)
                           VALUES (%s, %s, %s, %s, %s)
                           ON CONFLICT (org_id, clio_webhook_id) DO NOTHING""",
                        (ORG_ID, webhook_id, model, WEBHOOK_URL, data.get('expires_at', expires_at))
                    )
                conn.commit()
                log_audit(conn, ORG_ID, 'webhook-register', 'system.webhook_registered',
                          {'model': model, 'webhook_id': webhook_id})
                registered.append({'model': model, 'id': webhook_id})
                logger.info("Registered webhook: model=%s id=%s", model, webhook_id)
            else:
                logger.error("Failed to register %s: %s %s", model, resp.status_code, resp.text[:300])
        except Exception as exc:
            logger.error("Error registering %s: %s", model, exc)

    # Self-delete per Firm OS convention (temp Lambdas must self-delete)
    try:
        boto3.client('lambda', region_name=REGION).delete_function(FunctionName=context.function_name)
    except Exception as exc:
        logger.warning("Self-delete failed: %s", exc)

    return {'registered': registered, 'count': len(registered)}
