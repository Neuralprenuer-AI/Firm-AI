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
RENEW_BEFORE_DAYS = 7
WEBHOOK_TTL_DAYS = 30


def lambda_handler(event, context):
    conn = get_connection()
    now = datetime.now(timezone.utc)
    cutoff = now + timedelta(days=RENEW_BEFORE_DAYS)

    with conn.cursor() as cur:
        cur.execute(
            """SELECT ws.id, ws.org_id, ws.clio_webhook_id, ws.model,
                      o.clio_access_token
               FROM firm_os.clio_webhook_subscriptions ws
               JOIN firm_os.organizations o ON o.org_id = ws.org_id AND o.status = 'active'
               WHERE ws.expires_at < %s""",
            (cutoff,)
        )
        expiring = cur.fetchall()

    logger.info("webhook-renewer: %d expiring subscription(s)", len(expiring))
    renewed = 0
    failed = 0

    for sub in expiring:
        org_id = str(sub['org_id'])
        old_webhook_id = sub['clio_webhook_id']
        model = sub['model']
        token = sub.get('clio_access_token') or ''

        if not token:
            logger.warning("No token for org %s — skipping renewal", org_id)
            failed += 1
            continue

        new_expires = (now + timedelta(days=WEBHOOK_TTL_DAYS)).strftime('%Y-%m-%dT%H:%M:%SZ')
        headers = {'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'}

        try:
            requests.delete(f"{CLIO_API}/webhooks/{old_webhook_id}.json",
                            headers=headers, timeout=10)
        except Exception as exc:
            logger.warning("Could not delete old webhook %s: %s", old_webhook_id, exc)

        try:
            resp = requests.post(
                f"{CLIO_API}/webhooks.json",
                headers=headers,
                json={'data': {
                    'url': WEBHOOK_URL,
                    'model': model,
                    'events': ['created', 'updated', 'deleted'],
                    'expires_at': new_expires,
                }},
                timeout=10,
            )
            if resp.status_code not in (200, 201):
                logger.error("Re-registration failed org=%s model=%s: %s %s",
                             org_id, model, resp.status_code, resp.text[:300])
                failed += 1
                continue

            new_data = resp.json()['data']
            new_webhook_id = str(new_data['id'])
            new_expires_at = new_data.get('expires_at') or new_expires

            with conn.cursor() as cur:
                cur.execute(
                    """UPDATE firm_os.clio_webhook_subscriptions
                       SET clio_webhook_id = %s, expires_at = %s
                       WHERE id = %s""",
                    (new_webhook_id, new_expires_at, sub['id'])
                )
            conn.commit()
            log_audit(conn, org_id, 'clio-webhook-renewer', 'system.webhook_renewed',
                      {'old_id': old_webhook_id, 'new_id': new_webhook_id, 'model': model})
            renewed += 1
            logger.info("Renewed: org=%s model=%s %s -> %s", org_id, model, old_webhook_id, new_webhook_id)

        except Exception as exc:
            logger.error("Renewal error org=%s model=%s: %s", org_id, model, exc)
            failed += 1

    return {'renewed': renewed, 'failed': failed}
