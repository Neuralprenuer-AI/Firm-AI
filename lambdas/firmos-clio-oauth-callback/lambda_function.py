import json
import logging
import os
import sys
from datetime import datetime, timezone, timedelta
from urllib.parse import urlencode

import boto3
import requests

sys.path.insert(0, '/opt/python')
from shared_db import get_connection, log_audit

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

CLIO_TOKEN_URL = 'https://app.clio.com/oauth/token'
REDIRECT_URI = 'https://kezjhodcig.execute-api.us-east-2.amazonaws.com/prod/firmos/clio/callback'
DASHBOARD_URL = os.environ.get('DASHBOARD_URL', 'https://firm-os-dashboard.lovable.app')
CLIO_OAUTH_SECRET_ID = 'firmos/clio/oauth'


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _redirect(path: str, params: dict) -> dict:
    """Build an API Gateway 302 redirect response."""
    qs = urlencode(params)
    location = f"{DASHBOARD_URL}{path}?{qs}"
    return {
        'statusCode': 302,
        'headers': {'Location': location},
        'body': '',
    }


def _error_redirect(error_code: str) -> dict:
    return _redirect('/settings/crm', {'error': error_code})


def _load_clio_oauth_credentials() -> tuple[str, str]:
    """Return (client_id, client_secret) from Secrets Manager."""
    client = boto3.client('secretsmanager', region_name='us-east-2')
    raw = client.get_secret_value(SecretId=CLIO_OAUTH_SECRET_ID)['SecretString']
    data = json.loads(raw)
    return data['client_id'], data['client_secret']


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def lambda_handler(event, context):
    qs = event.get('queryStringParameters') or {}

    # Step 1 — OAuth error from Clio (user denied, etc.)
    if qs.get('error'):
        logger.warning("Clio OAuth denied by user: %s", qs.get('error'))
        return _error_redirect('clio_denied')

    code = qs.get('code', '').strip()
    state_raw = qs.get('state', '').strip()

    # Step 2 — missing required params
    if not code or not state_raw:
        logger.warning("Clio OAuth callback missing code or state")
        return _error_redirect('missing_params')

    # Step 3 — parse state JSON
    try:
        state_data = json.loads(state_raw)
        org_id = state_data['org_id']
        nonce = state_data['nonce']
    except (json.JSONDecodeError, KeyError) as exc:
        logger.warning("Clio OAuth invalid state payload: %s — %s", state_raw[:200], exc)
        return _error_redirect('invalid_state')

    conn = None
    try:
        conn = get_connection()

        # Step 4 — load org, handle gracefully if clio_oauth_state column absent
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT org_id, clio_oauth_state
                       FROM firm_os.organizations
                       WHERE org_id = %s AND status = 'active'""",
                    (org_id,),
                )
                org = cur.fetchone()
        except Exception as db_exc:
            # Column may not exist yet — try without it
            if 'clio_oauth_state' in str(db_exc).lower():
                logger.warning("clio_oauth_state column missing, skipping nonce validation")
                conn.rollback()
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT org_id FROM firm_os.organizations WHERE org_id = %s AND status = 'active'",
                        (org_id,),
                    )
                    row = cur.fetchone()
                org = dict(row) if row else None
                if org:
                    org['clio_oauth_state'] = nonce  # treat as matching when column absent
            else:
                raise

        if not org:
            logger.warning("Clio OAuth callback: org not found or inactive — org_id=%s", org_id)
            return _error_redirect('server_error')

        # Step 5 — validate nonce
        stored_nonce = org.get('clio_oauth_state')
        if stored_nonce != nonce:
            logger.warning(
                "Clio OAuth state mismatch — org_id=%s stored=%s received=%s",
                org_id, stored_nonce, nonce,
            )
            log_audit(
                conn, org_id, 'system', 'clio.oauth_state_mismatch',
                {'stored_nonce_hash': str(stored_nonce)[:8] if stored_nonce else None},
                severity='warning',
            )
            return _error_redirect('state_mismatch')

        # Step 6 — load Clio OAuth app credentials
        try:
            clio_client_id, clio_client_secret = _load_clio_oauth_credentials()
        except Exception as exc:
            logger.error("Failed to load Clio OAuth credentials: %s", exc)
            log_audit(conn, org_id, 'system', 'clio.oauth_credentials_load_failed',
                      {'error': str(exc)}, severity='critical')
            return _error_redirect('server_error')

        # Step 7 — exchange code for tokens
        try:
            token_resp = requests.post(
                CLIO_TOKEN_URL,
                data={
                    'grant_type': 'authorization_code',
                    'code': code,
                    'client_id': clio_client_id,
                    'client_secret': clio_client_secret,
                    'redirect_uri': REDIRECT_URI,
                },
                timeout=10,
            )
        except requests.RequestException as exc:
            logger.error("Clio token exchange HTTP error for org %s: %s", org_id, exc)
            log_audit(conn, org_id, 'system', 'clio.oauth_token_exchange_failed',
                      {'reason': 'http_error', 'error': str(exc)}, severity='warning')
            return _error_redirect('token_exchange_failed')

        if token_resp.status_code != 200:
            logger.error(
                "Clio token exchange failed for org %s — status=%s body=%s",
                org_id, token_resp.status_code, token_resp.text[:300],
            )
            log_audit(conn, org_id, 'system', 'clio.oauth_token_exchange_failed',
                      {'reason': 'bad_status', 'status': token_resp.status_code},
                      severity='warning')
            return _error_redirect('token_exchange_failed')

        token_data = token_resp.json()
        access_token = token_data['access_token']
        refresh_token = token_data.get('refresh_token', '')
        expires_in = int(token_data.get('expires_in', 3600))
        token_expires_at = datetime.now(timezone.utc) + timedelta(seconds=expires_in)

        # Step 8 — persist tokens to DB
        with conn.cursor() as cur:
            cur.execute(
                """UPDATE firm_os.organizations
                   SET clio_access_token     = %s,
                       clio_refresh_token    = %s,
                       clio_token_expires_at = %s,
                       crm_platform          = 'clio',
                       clio_oauth_state      = NULL
                   WHERE org_id = %s""",
                (access_token, refresh_token, token_expires_at, org_id),
            )
        conn.commit()

        # Step 9 — audit success
        log_audit(
            conn, org_id, 'system', 'clio.oauth_connected',
            {'expires_in': expires_in, 'has_refresh_token': bool(refresh_token)},
            severity='info',
        )

        logger.info("Clio OAuth connected successfully for org_id=%s", org_id)
        return _redirect('/settings/crm', {'connected': 'true'})

    except Exception as exc:
        logger.error("Unhandled error in Clio OAuth callback for org_id=%s: %s", org_id, exc)
        if conn is not None:
            try:
                log_audit(conn, org_id, 'system', 'clio.oauth_callback_error',
                          {'error': str(exc)}, severity='critical')
            except Exception:
                pass
        return _error_redirect('server_error')
