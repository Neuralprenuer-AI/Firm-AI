# lambdas/firmos-voice-tools/lambda_function.py
import json
import logging
import sys
import urllib.parse
import uuid
from datetime import date, datetime, timezone

import boto3
import requests

sys.path.insert(0, '/opt/python')
from shared_db import get_connection, log_audit

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

REGION = 'us-east-2'
CLIO_API = 'https://app.clio.com/api/v4'
TIMEOUT = 8

_secret_cache: dict = {}


def _get_secret(secret_id: str) -> dict:
    if secret_id not in _secret_cache:
        raw = boto3.client('secretsmanager', region_name=REGION)\
            .get_secret_value(SecretId=secret_id)['SecretString']
        _secret_cache[secret_id] = json.loads(raw)
    return _secret_cache[secret_id]


def _resp(status: int, body: dict) -> dict:
    return {
        'statusCode': status,
        'headers': {'Content-Type': 'application/json'},
        'body': json.dumps(body, default=str),
    }


def _verify_secret(event: dict) -> bool:
    headers = {k.lower(): v for k, v in (event.get('headers') or {}).items()}
    provided = headers.get('x-voice-secret', '')
    try:
        expected = _get_secret('firmos/voice/webhook-secret')['secret']
        return provided == expected
    except Exception as exc:
        logger.error("Secret fetch failed: %s", exc)
        return False


def handle_lookup_caller(conn, params: dict) -> dict:
    phone = (params.get('phone') or '').strip()
    org_id = (params.get('org_id') or '').strip()
    if not phone or not org_id:
        return _resp(400, {'error': 'phone and org_id required'})

    with conn.cursor() as cur:
        cur.execute(
            "SELECT contact_id, name, preferred_language "
            "FROM firm_os.contacts WHERE org_id = %s AND phone = %s",
            (org_id, phone),
        )
        contact = cur.fetchone()

    if not contact:
        return _resp(200, {'is_existing_client': False})

    contact_id = str(contact['contact_id'])
    result: dict = {
        'is_existing_client': True,
        'contact_id': contact_id,
        'name': contact.get('name'),
        'language': contact.get('preferred_language', 'en'),
    }

    with conn.cursor() as cur:
        cur.execute(
            "SELECT matter_display_number, matter_status, responsible_attorney_name "
            "FROM firm_os.case_status_cache WHERE org_id = %s AND contact_id = %s "
            "ORDER BY last_synced_at DESC LIMIT 1",
            (org_id, contact_id),
        )
        matter = cur.fetchone()

    if matter:
        status_str = f"Matter {matter['matter_display_number']} — {matter['matter_status']}"
        if matter.get('responsible_attorney_name'):
            status_str += f", Attorney: {matter['responsible_attorney_name']}"
        result['case_status'] = status_str

    with conn.cursor() as cur:
        cur.execute(
            "SELECT summary, start_at FROM firm_os.clio_calendar_entries "
            "WHERE org_id = %s AND contact_id = %s AND start_at > NOW() "
            "ORDER BY start_at ASC LIMIT 1",
            (org_id, contact_id),
        )
        appt = cur.fetchone()

    if appt and appt.get('start_at'):
        date_str = appt['start_at'].strftime('%B %d at %I:%M %p')
        result['upcoming_appointment'] = f"{appt['summary']} on {date_str}"

    return _resp(200, result)


def handle_complete_intake(conn, body: dict) -> dict:
    org_id = (body.get('org_id') or '').strip()
    phone = (body.get('phone') or '').strip()
    name = (body.get('name') or '').strip()
    issue = (body.get('issue') or '').strip()
    incident_date = (body.get('incident_date') or '').strip()
    has_attorney = bool(body.get('has_attorney', False))
    language = body.get('language', 'en')

    if not all([org_id, phone, name, issue]):
        return _resp(400, {'error': 'org_id, phone, name, issue required'})

    with conn.cursor() as cur:
        cur.execute(
            "SELECT contact_id FROM firm_os.contacts WHERE org_id = %s AND phone = %s",
            (org_id, phone),
        )
        row = cur.fetchone()

    if row:
        contact_id = str(row['contact_id'])
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE firm_os.contacts SET "
                "name = COALESCE(%s, name), preferred_language = %s "
                "WHERE org_id = %s AND contact_id = %s",
                (name or None, language, org_id, contact_id),
            )
        conn.commit()
    else:
        contact_id = str(uuid.uuid4())
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO firm_os.contacts "
                "(contact_id, org_id, phone, name, preferred_language, status, created_at) "
                "VALUES (%s, %s, %s, %s, %s, 'active', NOW())",
                (contact_id, org_id, phone, name, language),
            )
        conn.commit()

    conv_id = str(uuid.uuid4())
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO firm_os.conversations "
            "(conversation_id, org_id, contact_id, channel, state, started_at) "
            "VALUES (%s, %s, %s, 'voice', 'complete', NOW())",
            (conv_id, org_id, contact_id),
        )
    conn.commit()

    intake_id = str(uuid.uuid4())
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO firm_os.intake_records "
            "(id, org_id, contact_id, conversation_id, intake_name, intake_issue, "
            "intake_date, intake_has_attorney, intake_language, is_complete, completed_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, TRUE, NOW())",
            (intake_id, org_id, contact_id, conv_id,
             name, issue, incident_date, has_attorney, language),
        )
    conn.commit()

    try:
        boto3.client('lambda', region_name=REGION).invoke(
            FunctionName='firmos-crm-push',
            InvocationType='Event',
            Payload=json.dumps({
                'org_id': org_id,
                'contact_id': contact_id,
                'intake_id': intake_id,
            }).encode(),
        )
    except Exception as exc:
        logger.warning("crm-push invoke failed: %s", exc)

    log_audit(conn, org_id, 'voice-tools', 'voice.intake_completed', {'contact_id': contact_id})
    return _resp(200, {'contact_id': contact_id, 'intake_id': intake_id, 'success': True})


def handle_check_availability(conn, params: dict) -> dict:
    org_id = (params.get('org_id') or '').strip()
    date_str = (params.get('date') or '').strip()

    if not org_id or not date_str:
        return _resp(400, {'error': 'org_id and date required'})

    try:
        target_date = date.fromisoformat(date_str)
    except ValueError:
        return _resp(400, {'error': 'date must be YYYY-MM-DD'})

    with conn.cursor() as cur:
        cur.execute(
            "SELECT summary, start_at FROM firm_os.clio_calendar_entries "
            "WHERE org_id = %s AND DATE(start_at) = %s ORDER BY start_at ASC",
            (org_id, target_date),
        )
        booked = cur.fetchall()

    booked_times = [
        e['start_at'].strftime('%H:%M') for e in booked if e.get('start_at')
    ]
    all_slots = [f'{h:02d}:00' for h in range(9, 17)]
    open_slots = [s for s in all_slots if s not in booked_times]

    return _resp(200, {
        'date': date_str,
        'booked_slots': booked_times,
        'suggested_open': open_slots[:4],
    })


def handle_book_appointment(conn, body: dict) -> dict:
    org_id = (body.get('org_id') or '').strip()
    contact_id = (body.get('contact_id') or '').strip()
    summary = (body.get('summary') or 'Consultation').strip()
    start_at = (body.get('start_at') or '').strip()
    end_at = (body.get('end_at') or '').strip()

    if not all([org_id, contact_id, start_at, end_at]):
        return _resp(400, {'error': 'org_id, contact_id, start_at, end_at required'})

    with conn.cursor() as cur:
        cur.execute(
            "SELECT clio_access_token FROM firm_os.organizations "
            "WHERE org_id = %s AND status = 'active'",
            (org_id,),
        )
        org = cur.fetchone()

    if not org or not org.get('clio_access_token'):
        return _resp(503, {'error': 'Clio not connected'})

    with conn.cursor() as cur:
        cur.execute(
            "SELECT clio_matter_id FROM firm_os.case_status_cache "
            "WHERE org_id = %s AND contact_id = %s ORDER BY last_synced_at DESC LIMIT 1",
            (org_id, contact_id),
        )
        matter_row = cur.fetchone()

    clio_matter_id = matter_row['clio_matter_id'] if matter_row else None
    clio_entry_id = None

    try:
        payload: dict = {
            'data': {
                'summary': summary,
                'start_at': start_at,
                'end_at': end_at,
                'all_day': False,
            }
        }
        if clio_matter_id:
            payload['data']['matter'] = {'id': int(clio_matter_id)}

        clio_resp = requests.post(
            f'{CLIO_API}/calendar_entries.json',
            headers={
                'Authorization': f"Bearer {org['clio_access_token']}",
                'Content-Type': 'application/json',
            },
            json=payload,
            timeout=TIMEOUT,
        )
        if clio_resp.status_code in (200, 201):
            clio_entry_id = str(clio_resp.json()['data']['id'])
        else:
            logger.warning("Clio calendar create %s: %s", clio_resp.status_code, clio_resp.text[:200])
    except Exception as exc:
        logger.warning("Clio calendar exception: %s", exc)

    entry_id_for_db = clio_entry_id or f'voice-{uuid.uuid4()}'
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO firm_os.clio_calendar_entries "
            "(org_id, contact_id, clio_matter_id, clio_entry_id, summary, start_at, end_at, all_day, synced_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, FALSE, NOW()) "
            "ON CONFLICT (org_id, clio_entry_id) DO NOTHING",
            (org_id, contact_id, clio_matter_id, entry_id_for_db, summary, start_at, end_at),
        )
    conn.commit()

    log_audit(conn, org_id, 'voice-tools', 'voice.appointment_booked', {
        'contact_id': contact_id, 'summary': summary, 'start_at': start_at,
    })
    return _resp(200, {'clio_entry_id': clio_entry_id, 'confirmed': True})


def handle_escalate_transfer(conn, body: dict) -> dict:
    org_id = (body.get('org_id') or '').strip()
    contact_id = body.get('contact_id') or ''
    reason = body.get('reason', 'caller_requested')

    if not org_id:
        return _resp(400, {'error': 'org_id required'})

    with conn.cursor() as cur:
        cur.execute(
            "SELECT emergency_contact_number FROM firm_os.organizations "
            "WHERE org_id = %s AND status = 'active'",
            (org_id,),
        )
        org = cur.fetchone()

    if not org or not org.get('emergency_contact_number'):
        return _resp(503, {'error': 'No emergency contact configured'})

    with conn.cursor() as cur:
        cur.execute(
            "SELECT phone FROM firm_os.org_users "
            "WHERE org_id = %s AND escalation_routing = TRUE AND status = 'active' "
            "ORDER BY created_at ASC LIMIT 1",
            (org_id,),
        )
        oncall = cur.fetchone()

    transfer_to = (oncall or {}).get('phone') or org['emergency_contact_number']

    if contact_id:
        try:
            boto3.client('lambda', region_name=REGION).invoke(
                FunctionName='firmos-escalation',
                InvocationType='Event',
                Payload=json.dumps({
                    'org_id': org_id,
                    'contact_id': str(contact_id),
                    'triggered_keyword': reason,
                    'message_body': f'Voice escalation: {reason}',
                    'channel': 'voice',
                }).encode(),
            )
        except Exception as exc:
            logger.warning("escalation invoke failed: %s", exc)

    log_audit(conn, org_id, 'voice-tools', 'voice.escalation_triggered', {
        'contact_id': str(contact_id), 'reason': reason, 'transfer_to': transfer_to,
    })
    return _resp(200, {'transfer_to': transfer_to})


def _parse_qs(event: dict) -> dict:
    return dict(urllib.parse.parse_qsl(event.get('rawQueryString', '') or ''))


def _parse_body(event: dict) -> dict:
    try:
        return json.loads(event.get('body') or '{}')
    except Exception:
        return {}


ROUTES: dict = {
    ('GET', '/firmos/voice/caller'): lambda c, e: handle_lookup_caller(c, _parse_qs(e)),
    ('POST', '/firmos/voice/intake'): lambda c, e: handle_complete_intake(c, _parse_body(e)),
    ('GET', '/firmos/voice/availability'): lambda c, e: handle_check_availability(c, _parse_qs(e)),
    ('POST', '/firmos/voice/appointment'): lambda c, e: handle_book_appointment(c, _parse_body(e)),
    ('POST', '/firmos/voice/escalate'): lambda c, e: handle_escalate_transfer(c, _parse_body(e)),
}


def lambda_handler(event, context):
    if not _verify_secret(event):
        return _resp(401, {'error': 'unauthorized'})

    method = (
        event.get('httpMethod')
        or (event.get('requestContext') or {}).get('http', {}).get('method', '')
    ).upper()
    path = event.get('path') or event.get('rawPath', '')

    handler = ROUTES.get((method, path))
    if not handler:
        return _resp(404, {'error': f'No route: {method} {path}'})

    conn = get_connection()
    return handler(conn, event)
