import json
import boto3
import hashlib
import requests
import sys
import logging
from datetime import datetime, timezone, timedelta

sys.path.insert(0, '/opt/python')
from shared_db import get_connection, log_audit

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

CLIO_API = 'https://app.clio.com/api/v4'
CLIO_TOKEN_URL = 'https://app.clio.com/oauth/token'
TOKEN_REFRESH_BUFFER_SECONDS = 300
CLIO_REQUEST_TIMEOUT = 10


# ---------------------------------------------------------------------------
# Token helpers
# ---------------------------------------------------------------------------

def _load_clio_credentials(org_id: str) -> dict:
    """Fetch Clio OAuth credentials from Secrets Manager."""
    client = boto3.client('secretsmanager', region_name='us-east-2')
    secret_id = f'firmos/{org_id}/clio-credentials'
    raw = client.get_secret_value(SecretId=secret_id)['SecretString']
    return json.loads(raw)


def _refresh_clio_token(conn, org: dict) -> dict | None:
    """
    Attempt a Clio OAuth token refresh.

    Returns updated org dict on success, None on failure.
    Logs system.clio_token_refresh_failed (severity='critical') on failure.
    """
    org_id = str(org['org_id'])
    try:
        creds = _load_clio_credentials(org_id)
    except Exception as exc:
        logger.error("Could not load Clio credentials for org %s: %s", org_id, exc)
        log_audit(conn, org_id, 'clio-sync', 'system.clio_token_refresh_failed',
                  {'reason': 'secrets_load_error', 'error': str(exc)}, severity='critical')
        return None

    try:
        resp = requests.post(
            CLIO_TOKEN_URL,
            data={
                'grant_type': 'refresh_token',
                'refresh_token': creds['refresh_token'],
                'client_id': creds['client_id'],
                'client_secret': creds['client_secret'],
            },
            timeout=CLIO_REQUEST_TIMEOUT,
        )
    except requests.RequestException as exc:
        logger.error("Token refresh HTTP error for org %s: %s", org_id, exc)
        log_audit(conn, org_id, 'clio-sync', 'system.clio_token_refresh_failed',
                  {'reason': 'http_error', 'error': str(exc)}, severity='critical')
        return None

    if resp.status_code != 200:
        logger.error("Token refresh failed for org %s — status %s", org_id, resp.status_code)
        log_audit(conn, org_id, 'clio-sync', 'system.clio_token_refresh_failed',
                  {'reason': 'bad_status', 'status': resp.status_code, 'body': resp.text[:500]},
                  severity='critical')
        return None

    token_data = resp.json()
    new_access = token_data['access_token']
    new_refresh = token_data.get('refresh_token', creds['refresh_token'])
    expires_in = token_data.get('expires_in', 3600)
    new_expires_at = datetime.now(timezone.utc) + timedelta(seconds=expires_in)

    with conn.cursor() as cur:
        cur.execute(
            """UPDATE firm_os.organizations
               SET clio_access_token     = %s,
                   clio_refresh_token    = %s,
                   clio_token_expires_at = %s
               WHERE org_id = %s""",
            (new_access, new_refresh, new_expires_at, org_id),
        )
    conn.commit()

    logger.info("Refreshed Clio token for org %s", org_id)
    return {**dict(org),
            'clio_access_token': new_access,
            'clio_refresh_token': new_refresh,
            'clio_token_expires_at': new_expires_at}


def _get_valid_token(conn, org: dict) -> str | None:
    """
    Return a valid Clio access token for the org, refreshing proactively if
    within TOKEN_REFRESH_BUFFER_SECONDS of expiry.

    Returns None when the token is absent, hard-expired, or refresh fails.
    Logs system.clio_token_expired for absent / hard-expired cases.
    """
    org_id = str(org['org_id'])
    expires_at = org.get('clio_token_expires_at')
    now = datetime.now(timezone.utc)

    if not org.get('clio_access_token'):
        log_audit(conn, org_id, 'clio-sync', 'system.clio_token_expired',
                  {'reason': 'no_access_token'}, severity='warning')
        return None

    # Normalise naive timestamps that may come from psycopg2
    if expires_at is not None and expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)

    if expires_at is None or now >= expires_at:
        log_audit(conn, org_id, 'clio-sync', 'system.clio_token_expired',
                  {'clio_token_expires_at': str(expires_at)}, severity='warning')
        return None

    # Proactive refresh inside the buffer window
    if (expires_at - now).total_seconds() <= TOKEN_REFRESH_BUFFER_SECONDS:
        refreshed = _refresh_clio_token(conn, org)
        if refreshed is None:
            return None
        return refreshed['clio_access_token']

    return org['clio_access_token']


# ---------------------------------------------------------------------------
# Matter sync core
# ---------------------------------------------------------------------------

def _build_notes_text(matter: dict) -> str:
    """Produce a deterministic, human-readable summary of a Clio matter."""
    practice_area_name = ''
    if matter.get('practice_area') and isinstance(matter['practice_area'], dict):
        practice_area_name = matter['practice_area'].get('name', '')

    lines = [
        f"Matter ID: {matter.get('id', '')}",
        f"Display Number: {matter.get('display_number', '')}",
        f"Description: {matter.get('description', '')}",
        f"Status: {matter.get('status', '')}",
        f"Practice Area: {practice_area_name}",
        f"Close Date: {matter.get('close_date', '')}",
    ]
    for cf in (matter.get('custom_field_values') or []):
        field_name = cf.get('field_name') or (cf.get('custom_field') or {}).get('name', '')
        if field_name:
            lines.append(f"Custom — {field_name}: {cf.get('value', '')}")
    return '\n'.join(lines)


def _upsert_matter(conn, org_id: str, contact_id: str, matter: dict) -> None:
    """
    Upsert a Clio matter into case_status_cache.
    The DO UPDATE fires only when the notes_hash has changed, avoiding
    unnecessary writes on unchanged data.
    """
    clio_matter_id = str(matter['id'])
    notes_text = _build_notes_text(matter)
    notes_hash = hashlib.sha256(notes_text.encode()).hexdigest()
    matter_display_number = matter.get('display_number', '')
    matter_status = matter.get('status', '')

    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO firm_os.case_status_cache
                   (org_id, contact_id, clio_matter_id, matter_display_number,
                    matter_status, notes_text, notes_hash, last_synced_at)
               VALUES (%s, %s, %s, %s, %s, %s, %s, NOW())
               ON CONFLICT (org_id, clio_matter_id)
               DO UPDATE SET
                   contact_id            = EXCLUDED.contact_id,
                   matter_display_number = EXCLUDED.matter_display_number,
                   matter_status         = EXCLUDED.matter_status,
                   notes_text            = EXCLUDED.notes_text,
                   notes_hash            = EXCLUDED.notes_hash,
                   last_synced_at        = NOW()
               WHERE firm_os.case_status_cache.notes_hash != EXCLUDED.notes_hash""",
            (org_id, contact_id, clio_matter_id, matter_display_number,
             matter_status, notes_text, notes_hash),
        )
    conn.commit()


def _sync_contact_matters(conn, org_id: str, contact_id: str, clio_contact_id: str, token: str) -> int:
    """
    Fetch all open Clio matters for one contact and upsert them.

    Raises RuntimeError on Clio API failure so the caller can log and continue.
    Returns the count of matters processed.
    """
    url = (
        f"{CLIO_API}/matters.json"
        f"?client_id={clio_contact_id}"
        f"&status=open"
        f"&fields=id,display_number,description,status,practice_area,close_date,custom_field_values"
    )
    headers = {'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'}

    try:
        resp = requests.get(url, headers=headers, timeout=CLIO_REQUEST_TIMEOUT)
    except requests.RequestException as exc:
        raise RuntimeError(f"Clio HTTP error: {exc}") from exc

    if resp.status_code != 200:
        raise RuntimeError(f"Clio returned {resp.status_code}: {resp.text[:300]}")

    matters = resp.json().get('data', [])
    for matter in matters:
        _upsert_matter(conn, org_id, contact_id, matter)

    logger.info("Synced %d matter(s) for contact %s (org %s)", len(matters), contact_id, org_id)
    return len(matters)


# ---------------------------------------------------------------------------
# Mode A — full org scan (EventBridge)
# ---------------------------------------------------------------------------

def _handle_scan(conn) -> dict:
    """
    Scan ALL active orgs with crm_platform='clio'.
    For each, fetch every contact that has a clio_contact_id and sync their
    open matters. Errors per-org or per-contact are logged and skipped;
    the scan always completes.
    """
    with conn.cursor() as cur:
        cur.execute(
            """SELECT org_id, name, clio_access_token, clio_refresh_token,
                      clio_token_expires_at, practice_area, secret_arn
               FROM firm_os.organizations
               WHERE status = 'active' AND crm_platform = 'clio'"""
        )
        orgs = cur.fetchall()

    logger.info("Scan mode: processing %d Clio org(s)", len(orgs))
    synced_orgs = 0
    skipped_orgs = 0

    for org in orgs:
        org_id = str(org['org_id'])
        try:
            token = _get_valid_token(conn, org)
            if token is None:
                skipped_orgs += 1
                continue

            with conn.cursor() as cur:
                cur.execute(
                    """SELECT contact_id, clio_contact_id
                       FROM firm_os.contacts
                       WHERE org_id = %s
                         AND clio_contact_id IS NOT NULL
                         AND clio_contact_id != ''""",
                    (org_id,),
                )
                contacts = cur.fetchall()

            for contact_row in contacts:
                contact_id = str(contact_row['contact_id'])
                clio_contact_id = contact_row['clio_contact_id']
                try:
                    _sync_contact_matters(conn, org_id, contact_id, clio_contact_id, token)
                except RuntimeError as exc:
                    logger.error(
                        "Clio sync failed for org %s contact %s: %s",
                        org_id, contact_id, exc,
                    )
                    log_audit(conn, org_id, 'clio-sync', 'system.clio_sync_failed',
                              {'contact_id': contact_id, 'error': str(exc)},
                              severity='warning')

            synced_orgs += 1

        except Exception as exc:
            logger.error("Unexpected error processing org %s: %s", org_id, exc)
            try:
                log_audit(conn, org_id, 'clio-sync', 'system.clio_sync_failed',
                          {'error': str(exc)}, severity='warning')
            except Exception:
                pass
            skipped_orgs += 1

    return {'mode': 'scan', 'synced_orgs': synced_orgs, 'skipped_orgs': skipped_orgs}


# ---------------------------------------------------------------------------
# Mode B — single contact (called from firmos-intake-agent)
# ---------------------------------------------------------------------------

def _handle_single_contact(conn, event: dict) -> dict:
    """
    Sync matters for a single contact triggered at INTAKE_COMPLETE.

    Because crm-push runs concurrently and may not yet have created the Clio
    contact, a missing clio_contact_id is a soft skip rather than an error.
    """
    org_id = event.get('org_id')
    if not org_id:
        return {'statusCode': 400, 'body': json.dumps({'error': 'Missing required field: org_id'})}
    contact_id = event.get('contact_id')
    if not contact_id:
        return {'statusCode': 400, 'body': json.dumps({'error': 'Missing required field: contact_id'})}
    intake_id = event.get('intake_id')
    if not intake_id:
        return {'statusCode': 400, 'body': json.dumps({'error': 'Missing required field: intake_id'})}
    org_id = str(org_id)
    contact_id = str(contact_id)
    intake_id = str(intake_id)

    with conn.cursor() as cur:
        cur.execute(
            """SELECT org_id, name, clio_access_token, clio_refresh_token,
                      clio_token_expires_at, crm_platform, practice_area
               FROM firm_os.organizations
               WHERE org_id = %s AND status = 'active'""",
            (org_id,),
        )
        org = cur.fetchone()

    if not org:
        logger.warning("clio-sync single: org not found or inactive — org_id=%s", org_id)
        return {'mode': 'single', 'skipped': True, 'reason': 'org_not_found'}

    if org.get('crm_platform') != 'clio':
        logger.info("clio-sync single: org %s crm_platform=%s, skipping", org_id, org.get('crm_platform'))
        return {'mode': 'single', 'skipped': True, 'reason': 'crm_platform_not_clio'}

    token = _get_valid_token(conn, org)
    if token is None:
        return {'mode': 'single', 'skipped': True, 'reason': 'token_invalid'}

    with conn.cursor() as cur:
        cur.execute(
            """SELECT contact_id, clio_contact_id
               FROM firm_os.contacts
               WHERE contact_id = %s AND org_id = %s""",
            (contact_id, org_id),
        )
        contact = cur.fetchone()

    if not contact:
        logger.warning(
            "clio-sync single: contact not found — contact_id=%s org_id=%s",
            contact_id, org_id,
        )
        return {'mode': 'single', 'skipped': True, 'reason': 'contact_not_found'}

    clio_contact_id = contact.get('clio_contact_id')
    if not clio_contact_id:
        # crm-push may not have run yet; this is expected and safe to skip
        logger.info(
            "clio-sync single: contact %s has no clio_contact_id yet, skipping matter sync",
            contact_id,
        )
        return {'mode': 'single', 'skipped': True, 'reason': 'no_clio_contact_id'}

    try:
        count = _sync_contact_matters(conn, org_id, contact_id, clio_contact_id, token)
    except RuntimeError as exc:
        logger.error("Clio sync failed for org %s contact %s: %s", org_id, contact_id, exc)
        log_audit(conn, org_id, 'clio-sync', 'system.clio_sync_failed',
                  {'contact_id': contact_id, 'intake_id': intake_id, 'error': str(exc)},
                  severity='warning')
        return {'mode': 'single', 'synced': False, 'error': str(exc)}

    return {'mode': 'single', 'synced': True, 'contact_id': contact_id, 'matters_synced': count}


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def lambda_handler(event, context):
    conn = get_connection()

    # Mode A: EventBridge scheduled scan
    if event.get('mode') == 'scan':
        result = _handle_scan(conn)
        logger.info("clio-sync scan complete: %s", result)
        return result

    # Mode B: single-contact trigger from intake-agent
    if event.get('org_id') and event.get('contact_id') and event.get('intake_id'):
        result = _handle_single_contact(conn, event)
        logger.info("clio-sync single complete: %s", result)
        return result

    logger.error("clio-sync: unrecognised event — %s", json.dumps(event)[:300])
    return {'statusCode': 400, 'body': json.dumps({'error': "Event must contain mode='scan' or {org_id, contact_id, intake_id}"})}
