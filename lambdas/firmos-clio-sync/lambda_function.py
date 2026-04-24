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

def _load_clio_app_credentials() -> dict:
    """Fetch global Clio OAuth app credentials (client_id + client_secret) from Secrets Manager."""
    client = boto3.client('secretsmanager', region_name='us-east-2')
    raw = client.get_secret_value(SecretId='firmos/clio/oauth')['SecretString']
    return json.loads(raw)


def _refresh_clio_token(conn, org: dict) -> dict | None:
    """
    Attempt a Clio OAuth token refresh using the org's stored refresh_token.

    Returns updated org dict on success, None on failure.
    Clio refresh response does NOT return a new refresh_token — keep existing.
    """
    org_id = str(org['org_id'])
    refresh_token = org.get('clio_refresh_token') or ''
    if not refresh_token:
        logger.error("No refresh token stored for org %s — cannot refresh", org_id)
        log_audit(conn, org_id, 'clio-sync', 'system.clio_token_refresh_failed',
                  {'reason': 'no_refresh_token'}, severity='critical')
        return None

    try:
        creds = _load_clio_app_credentials()
    except Exception as exc:
        logger.error("Could not load Clio app credentials: %s", exc)
        log_audit(conn, org_id, 'clio-sync', 'system.clio_token_refresh_failed',
                  {'reason': 'secrets_load_error', 'error': str(exc)}, severity='critical')
        return None

    try:
        resp = requests.post(
            CLIO_TOKEN_URL,
            data={
                'grant_type': 'refresh_token',
                'refresh_token': refresh_token,
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
    expires_in = token_data.get('expires_in', 604800)
    new_expires_at = datetime.now(timezone.utc) + timedelta(seconds=expires_in)

    with conn.cursor() as cur:
        cur.execute(
            """UPDATE firm_os.organizations
               SET clio_access_token     = %s,
                   clio_token_expires_at = %s
               WHERE org_id = %s""",
            (new_access, new_expires_at, org_id),
        )
    conn.commit()

    logger.info("Refreshed Clio token for org %s", org_id)
    return {**dict(org),
            'clio_access_token': new_access,
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

    responsible_name = ''
    if matter.get('responsible_attorney') and isinstance(matter['responsible_attorney'], dict):
        responsible_name = matter['responsible_attorney'].get('name', '')

    lines = [
        f"Matter ID: {matter.get('id', '')}",
        f"Display Number: {matter.get('display_number', '')}",
        f"Description: {matter.get('description', '')}",
        f"Status: {matter.get('status', '')}",
        f"Practice Area: {practice_area_name}",
        f"Open Date: {matter.get('open_date', '')}",
        f"Pending Date: {matter.get('pending_date', '')}",
        f"Close Date: {matter.get('close_date', '')}",
        f"Responsible Attorney: {responsible_name}",
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
    open_date = matter.get('open_date') or None
    pending_date = matter.get('pending_date') or None

    responsible_name = None
    if matter.get('responsible_attorney') and isinstance(matter['responsible_attorney'], dict):
        responsible_name = matter['responsible_attorney'].get('name') or None

    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO firm_os.case_status_cache
                   (org_id, contact_id, clio_matter_id, matter_display_number,
                    matter_status, notes_text, notes_hash, last_synced_at,
                    responsible_attorney_name, open_date, pending_date)
               VALUES (%s, %s, %s, %s, %s, %s, %s, NOW(), %s, %s, %s)
               ON CONFLICT (org_id, clio_matter_id)
               DO UPDATE SET
                   contact_id                = EXCLUDED.contact_id,
                   matter_display_number     = EXCLUDED.matter_display_number,
                   matter_status             = EXCLUDED.matter_status,
                   notes_text                = EXCLUDED.notes_text,
                   notes_hash                = EXCLUDED.notes_hash,
                   last_synced_at            = NOW(),
                   responsible_attorney_name = EXCLUDED.responsible_attorney_name,
                   open_date                 = EXCLUDED.open_date,
                   pending_date              = EXCLUDED.pending_date
               WHERE firm_os.case_status_cache.notes_hash != EXCLUDED.notes_hash""",
            (org_id, contact_id, clio_matter_id, matter_display_number,
             matter_status, notes_text, notes_hash,
             responsible_name, open_date, pending_date),
        )
    conn.commit()


def _sync_notes(conn, org_id: str, contact_id: str, clio_matter_id: str, token: str) -> None:
    """Pull notes for a matter and upsert into clio_notes."""
    url = f"{CLIO_API}/notes.json?matter_id={clio_matter_id}&fields=id,subject,detail,date"
    headers = {'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'}
    try:
        resp = requests.get(url, headers=headers, timeout=CLIO_REQUEST_TIMEOUT)
        if resp.status_code != 200:
            logger.warning("notes fetch failed matter %s: %s", clio_matter_id, resp.status_code)
            return
        for note in resp.json().get('data', []):
            with conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO firm_os.clio_notes
                           (org_id, contact_id, clio_matter_id, clio_note_id, subject, detail, note_date, synced_at)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, NOW())
                       ON CONFLICT (org_id, clio_note_id) DO UPDATE SET
                           subject=EXCLUDED.subject, detail=EXCLUDED.detail,
                           note_date=EXCLUDED.note_date, synced_at=NOW()""",
                    (org_id, contact_id, clio_matter_id, str(note['id']),
                     note.get('subject'), note.get('detail'), note.get('date'))
                )
        conn.commit()
    except requests.RequestException as exc:
        logger.warning("notes HTTP error matter %s: %s", clio_matter_id, exc)


def _sync_communications(conn, org_id: str, contact_id: str, clio_matter_id: str, token: str) -> None:
    """Pull communications for a matter and upsert into clio_communications."""
    url = f"{CLIO_API}/communications.json?matter_id={clio_matter_id}&fields=id,type,subject,body,received_at"
    headers = {'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'}
    try:
        resp = requests.get(url, headers=headers, timeout=CLIO_REQUEST_TIMEOUT)
        if resp.status_code != 200:
            logger.warning("comms fetch failed matter %s: %s", clio_matter_id, resp.status_code)
            return
        for comm in resp.json().get('data', []):
            received_at = comm.get('received_at') or None
            with conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO firm_os.clio_communications
                           (org_id, contact_id, clio_matter_id, clio_comm_id, comm_type, subject, body, received_at, synced_at)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, NOW())
                       ON CONFLICT (org_id, clio_comm_id) DO UPDATE SET
                           comm_type=EXCLUDED.comm_type, subject=EXCLUDED.subject,
                           body=EXCLUDED.body, received_at=EXCLUDED.received_at, synced_at=NOW()""",
                    (org_id, contact_id, clio_matter_id, str(comm['id']),
                     comm.get('type'), comm.get('subject'), comm.get('body'), received_at)
                )
        conn.commit()
    except requests.RequestException as exc:
        logger.warning("comms HTTP error matter %s: %s", clio_matter_id, exc)


def _sync_calendar_entries(conn, org_id: str, contact_id: str, clio_matter_id: str, token: str) -> None:
    """Pull calendar entries for a matter and upsert into clio_calendar_entries."""
    url = f"{CLIO_API}/calendar_entries.json?matter_id={clio_matter_id}&fields=id,summary,start_at,end_at,all_day"
    headers = {'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'}
    try:
        resp = requests.get(url, headers=headers, timeout=CLIO_REQUEST_TIMEOUT)
        if resp.status_code != 200:
            logger.warning("calendar fetch failed matter %s: %s", clio_matter_id, resp.status_code)
            return
        for entry in resp.json().get('data', []):
            with conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO firm_os.clio_calendar_entries
                           (org_id, contact_id, clio_matter_id, clio_entry_id, summary, start_at, end_at, all_day, synced_at)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, NOW())
                       ON CONFLICT (org_id, clio_entry_id) DO UPDATE SET
                           summary=EXCLUDED.summary, start_at=EXCLUDED.start_at,
                           end_at=EXCLUDED.end_at, all_day=EXCLUDED.all_day, synced_at=NOW()""",
                    (org_id, contact_id, clio_matter_id, str(entry['id']),
                     entry.get('summary'), entry.get('start_at'), entry.get('end_at'),
                     entry.get('all_day', False))
                )
        conn.commit()
    except requests.RequestException as exc:
        logger.warning("calendar HTTP error matter %s: %s", clio_matter_id, exc)


def _sync_conversations(conn, org_id: str, clio_matter_id: str, token: str) -> None:
    """Pull conversations + messages for a matter."""
    url = f"{CLIO_API}/conversations.json?matter_id={clio_matter_id}&fields=id,subject"
    headers = {'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'}
    try:
        resp = requests.get(url, headers=headers, timeout=CLIO_REQUEST_TIMEOUT)
        if resp.status_code != 200:
            logger.warning("conversations fetch failed matter %s: %s", clio_matter_id, resp.status_code)
            return
        for conv in resp.json().get('data', []):
            clio_conv_id = str(conv['id'])
            with conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO firm_os.clio_conversations
                           (org_id, clio_matter_id, clio_conv_id, subject, synced_at)
                       VALUES (%s, %s, %s, %s, NOW())
                       ON CONFLICT (org_id, clio_conv_id) DO UPDATE SET
                           subject=EXCLUDED.subject, synced_at=NOW()""",
                    (org_id, clio_matter_id, clio_conv_id, conv.get('subject'))
                )
            conn.commit()
            msg_url = f"{CLIO_API}/conversation_messages.json?conversation_id={clio_conv_id}&fields=id,body,created_at,author{{name}}"
            try:
                msg_resp = requests.get(msg_url, headers=headers, timeout=CLIO_REQUEST_TIMEOUT)
                if msg_resp.status_code == 200:
                    for msg in msg_resp.json().get('data', []):
                        author_name = None
                        if isinstance(msg.get('author'), dict):
                            author_name = msg['author'].get('name')
                        with conn.cursor() as cur:
                            cur.execute(
                                """INSERT INTO firm_os.clio_conversation_messages
                                       (org_id, clio_conv_id, clio_msg_id, body, author_name, created_at, synced_at)
                                   VALUES (%s, %s, %s, %s, %s, %s, NOW())
                                   ON CONFLICT (org_id, clio_msg_id) DO UPDATE SET
                                       body=EXCLUDED.body, synced_at=NOW()""",
                                (org_id, clio_conv_id, str(msg['id']),
                                 msg.get('body'), author_name, msg.get('created_at'))
                            )
                    conn.commit()
            except requests.RequestException as exc:
                logger.warning("messages HTTP error conv %s: %s", clio_conv_id, exc)
    except requests.RequestException as exc:
        logger.warning("conversations HTTP error matter %s: %s", clio_matter_id, exc)


def _sync_contact_back(conn, org_id: str, contact_id: str, clio_contact_id: str, token: str) -> None:
    """Pull contact name/email/phone from Clio and update firm_os.contacts."""
    url = f"{CLIO_API}/contacts/{clio_contact_id}.json?fields=id,first_name,last_name,email_addresses,phone_numbers"
    headers = {'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'}
    try:
        resp = requests.get(url, headers=headers, timeout=CLIO_REQUEST_TIMEOUT)
        if resp.status_code != 200:
            logger.warning("contact sync-back failed %s: %s", clio_contact_id, resp.status_code)
            return
        data = resp.json().get('data', {})
        first = data.get('first_name', '') or ''
        last = data.get('last_name', '') or ''
        full_name = f"{first} {last}".strip() or None
        email = None
        for e in (data.get('email_addresses') or []):
            if e.get('default_email') or email is None:
                email = e.get('address')
        phone = None
        for p in (data.get('phone_numbers') or []):
            if p.get('default_number') or phone is None:
                phone = p.get('number')
        with conn.cursor() as cur:
            cur.execute(
                """UPDATE firm_os.contacts
                   SET name  = COALESCE(%s, name),
                       email = COALESCE(%s, email),
                       phone = COALESCE(%s, phone)
                   WHERE contact_id = %s AND org_id = %s""",
                (full_name, email, phone, contact_id, org_id)
            )
        conn.commit()
    except requests.RequestException as exc:
        logger.warning("contact sync-back HTTP error %s: %s", clio_contact_id, exc)


def _sync_contact_all(conn, org_id: str, contact_id: str, clio_contact_id: str, token: str) -> int:
    """Sync all Clio data for one contact: contact sync-back, matters, notes, comms, calendar, conversations."""
    _sync_contact_back(conn, org_id, contact_id, clio_contact_id, token)

    url = (
        f"{CLIO_API}/matters.json"
        f"?client_id={clio_contact_id}"
        f"&status=open"
        f"&fields=id,display_number,description,status,practice_area{{name}},"
        f"open_date,pending_date,close_date,"
        f"responsible_attorney{{name}},"
        f"custom_field_values{{field_name,value}}"
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
        clio_matter_id = str(matter['id'])
        _sync_notes(conn, org_id, contact_id, clio_matter_id, token)
        _sync_communications(conn, org_id, contact_id, clio_matter_id, token)
        _sync_calendar_entries(conn, org_id, contact_id, clio_matter_id, token)
        _sync_conversations(conn, org_id, clio_matter_id, token)

    logger.info("Full sync: %d matter(s) for contact %s (org %s)", len(matters), contact_id, org_id)
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
                    _sync_contact_all(conn, org_id, contact_id, clio_contact_id, token)
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
        count = _sync_contact_all(conn, org_id, contact_id, clio_contact_id, token)
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
