import json
import hmac
import hashlib
import sys
import logging

sys.path.insert(0, '/opt/python')
from shared_db import get_connection, log_audit

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)


def _resp(status: int, body: dict, extra_headers: dict | None = None) -> dict:
    headers = {'Content-Type': 'application/json'}
    if extra_headers:
        headers.update(extra_headers)
    return {'statusCode': status, 'headers': headers, 'body': json.dumps(body)}


def _verify_signature(body_bytes: bytes, signature: str, secret: str) -> bool:
    expected = hmac.new(secret.encode(), body_bytes, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


def _get_org_for_webhook(conn, clio_webhook_id: str):
    with conn.cursor() as cur:
        cur.execute(
            """SELECT ws.org_id, ws.hook_secret
               FROM firm_os.clio_webhook_subscriptions ws
               WHERE ws.clio_webhook_id = %s""",
            (clio_webhook_id,)
        )
        return cur.fetchone()


def _upsert_matter_event(conn, org_id: str, event_data: dict) -> None:
    clio_matter_id = str(event_data.get('id', ''))
    if not clio_matter_id:
        return
    status = event_data.get('status') or ''
    description = event_data.get('description') or ''
    with conn.cursor() as cur:
        cur.execute(
            """UPDATE firm_os.case_status_cache
               SET matter_status = %s, notes_text = %s, last_synced_at = NOW()
               WHERE org_id = %s AND clio_matter_id = %s""",
            (status, description, org_id, clio_matter_id)
        )
    conn.commit()
    logger.info("matter webhook upsert: org=%s matter=%s status=%s", org_id, clio_matter_id, status)


def _upsert_contact_event(conn, org_id: str, event_data: dict) -> None:
    clio_contact_id = str(event_data.get('id', ''))
    if not clio_contact_id:
        return
    first = event_data.get('first_name', '') or ''
    last = event_data.get('last_name', '') or ''
    full_name = f"{first} {last}".strip() or None
    email = None
    for e in (event_data.get('email_addresses') or []):
        if e.get('default_email') or email is None:
            email = e.get('address')
    phone = None
    for p in (event_data.get('phone_numbers') or []):
        if p.get('default_number') or phone is None:
            phone = p.get('number')
    with conn.cursor() as cur:
        cur.execute(
            """UPDATE firm_os.contacts
               SET name  = COALESCE(%s, name),
                   email = COALESCE(%s, email),
                   phone = COALESCE(%s, phone)
               WHERE clio_contact_id = %s AND org_id = %s""",
            (full_name, email, phone, clio_contact_id, org_id)
        )
    conn.commit()


def _upsert_note_event(conn, org_id: str, event_data: dict) -> None:
    clio_note_id = str(event_data.get('id', ''))
    matter_id = str((event_data.get('matter') or {}).get('id', '')) or None
    if not clio_note_id or not matter_id:
        return
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO firm_os.clio_notes
                   (org_id, clio_matter_id, clio_note_id, subject, detail, note_date, synced_at)
               VALUES (%s, %s, %s, %s, %s, %s, NOW())
               ON CONFLICT (org_id, clio_note_id) DO UPDATE SET
                   subject=EXCLUDED.subject, detail=EXCLUDED.detail,
                   note_date=EXCLUDED.note_date, synced_at=NOW()""",
            (org_id, matter_id, clio_note_id,
             event_data.get('subject'), event_data.get('detail'), event_data.get('date'))
        )
    conn.commit()


def _upsert_communication_event(conn, org_id: str, event_data: dict) -> None:
    clio_comm_id = str(event_data.get('id', ''))
    matter_id = str((event_data.get('matter') or {}).get('id', '')) or None
    if not clio_comm_id or not matter_id:
        return
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO firm_os.clio_communications
                   (org_id, clio_matter_id, clio_comm_id, comm_type, subject, body, received_at, synced_at)
               VALUES (%s, %s, %s, %s, %s, %s, %s, NOW())
               ON CONFLICT (org_id, clio_comm_id) DO UPDATE SET
                   comm_type=EXCLUDED.comm_type, subject=EXCLUDED.subject,
                   body=EXCLUDED.body, received_at=EXCLUDED.received_at, synced_at=NOW()""",
            (org_id, matter_id, clio_comm_id,
             event_data.get('type'), event_data.get('subject'),
             event_data.get('body'), event_data.get('received_at'))
        )
    conn.commit()


def _upsert_calendar_event(conn, org_id: str, event_data: dict) -> None:
    clio_entry_id = str(event_data.get('id', ''))
    matter_id = str((event_data.get('matter') or {}).get('id', '')) or None
    if not clio_entry_id or not matter_id:
        return
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO firm_os.clio_calendar_entries
                   (org_id, clio_matter_id, clio_entry_id, summary, start_at, end_at, all_day, synced_at)
               VALUES (%s, %s, %s, %s, %s, %s, %s, NOW())
               ON CONFLICT (org_id, clio_entry_id) DO UPDATE SET
                   summary=EXCLUDED.summary, start_at=EXCLUDED.start_at,
                   end_at=EXCLUDED.end_at, all_day=EXCLUDED.all_day, synced_at=NOW()""",
            (org_id, matter_id, clio_entry_id,
             event_data.get('summary'), event_data.get('start_at'),
             event_data.get('end_at'), event_data.get('all_day', False))
        )
    conn.commit()


MODEL_HANDLERS = {
    'matter': _upsert_matter_event,
    'contact': _upsert_contact_event,
    'note': _upsert_note_event,
    'communication': _upsert_communication_event,
    'calendar_entry': _upsert_calendar_event,
}


def lambda_handler(event, context):
    headers = {k.lower(): v for k, v in (event.get('headers') or {}).items()}
    body_str = event.get('body') or ''
    body_bytes = body_str.encode('utf-8')

    # Clio handshake
    hook_secret_header = headers.get('x-hook-secret')
    if hook_secret_header:
        logger.info("Clio webhook handshake received")
        return _resp(200, {'status': 'handshake_ok'}, {'X-Hook-Secret': hook_secret_header})

    try:
        payload = json.loads(body_str)
    except (json.JSONDecodeError, TypeError):
        return _resp(400, {'error': 'invalid_json'})

    webhook_id = str(payload.get('webhook_id', ''))
    model = payload.get('model', '')
    event_type = payload.get('event', '')
    event_data = payload.get('data', {})

    if not webhook_id or not model:
        return _resp(400, {'error': 'missing_webhook_id_or_model'})

    conn = get_connection()

    sub = _get_org_for_webhook(conn, webhook_id)
    if not sub:
        logger.warning("Unknown webhook_id=%s", webhook_id)
        return _resp(200, {'status': 'unknown_webhook'})

    org_id = str(sub['org_id'])
    hook_secret = sub.get('hook_secret') or ''

    signature = headers.get('x-hook-signature', '')
    if hook_secret and signature:
        if not _verify_signature(body_bytes, signature, hook_secret):
            logger.warning("Invalid signature for org=%s webhook=%s", org_id, webhook_id)
            log_audit(conn, org_id, 'clio-webhook', 'system.webhook_invalid_signature',
                      {'webhook_id': webhook_id}, severity='warning')
            return _resp(401, {'error': 'invalid_signature'})

    handler = MODEL_HANDLERS.get(model)
    if handler and event_type != 'deleted':
        try:
            handler(conn, org_id, event_data)
        except Exception as exc:
            logger.error("Webhook handler error org=%s model=%s: %s", org_id, model, exc)
            log_audit(conn, org_id, 'clio-webhook', 'system.webhook_handler_error',
                      {'model': model, 'event': event_type, 'error': str(exc)}, severity='warning')

    log_audit(conn, org_id, 'clio-webhook', f'system.webhook_{event_type}',
              {'model': model, 'webhook_id': webhook_id})

    return _resp(200, {'status': 'ok'})
