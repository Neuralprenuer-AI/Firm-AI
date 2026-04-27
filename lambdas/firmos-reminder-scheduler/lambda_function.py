import json
import boto3
import sys
import logging
from datetime import datetime, timezone, timedelta

sys.path.insert(0, '/opt/python')
from shared_db import get_connection, log_audit

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

REGION = 'us-east-2'


_secret_cache: dict = {}

ALLOWED_FLAG_COLS = {'reminder_sent_48h', 'reminder_sent_24h'}


def _invoke_send(secret_arn: str, org_id: str, conv_id: str, to_phone: str, body: str) -> None:
    if secret_arn not in _secret_cache:
        _secret_cache[secret_arn] = json.loads(
            boto3.client('secretsmanager', region_name=REGION)
            .get_secret_value(SecretId=secret_arn)['SecretString']
        )
    secret = _secret_cache[secret_arn]
    boto3.client('lambda', region_name=REGION).invoke(
        FunctionName='firmos-twilio-send',
        InvocationType='Event',
        Payload=json.dumps({
            'org_id': org_id,
            'to_phone': to_phone,
            'body': body,
            'conversation_id': conv_id,
            'subaccount_token': secret['twilio_auth_token'],
        }).encode()
    )


def _send_reminders(conn, window_start, window_end, flag_col: str, hours: int) -> tuple[int, int]:
    if flag_col not in ALLOWED_FLAG_COLS:
        raise ValueError(f"Invalid flag_col: {flag_col}")
    sent = 0
    errors = 0
    with conn.cursor() as cur:
        cur.execute(
            f"""SELECT ce.id, ce.org_id, ce.contact_id, ce.summary, ce.start_at,
                      c.phone, c.preferred_language,
                      o.secret_arn,
                      cv.conversation_id
               FROM firm_os.clio_calendar_entries ce
               JOIN firm_os.contacts c ON c.contact_id = ce.contact_id AND c.org_id = ce.org_id
               JOIN firm_os.organizations o ON o.org_id = ce.org_id AND o.status = 'active'
               LEFT JOIN firm_os.conversations cv
                   ON cv.contact_id = ce.contact_id AND cv.org_id = ce.org_id AND cv.status = 'active'
               WHERE ce.start_at BETWEEN %s AND %s
                 AND ce.{flag_col} = FALSE
                 AND ce.contact_id IS NOT NULL""",
            (window_start, window_end)
        )
        rows = cur.fetchall()

    for row in rows:
        try:
            lang = row.get('preferred_language') or 'en'
            summary = row.get('summary') or 'upcoming appointment'
            start_at = row['start_at']
            date_str = start_at.strftime('%B %d at %I:%M %p UTC') if start_at else 'soon'
            org_id = str(row['org_id'])

            if hours == 48:
                msg = (
                    f"Recordatorio: Tiene '{summary}' programado para {date_str}. Por favor confirme su asistencia."
                    if lang == 'es' else
                    f"Reminder: You have '{summary}' scheduled for {date_str}. Please confirm your attendance."
                )
            else:
                msg = (
                    f"Recordatorio urgente: '{summary}' es mañana {date_str}. ¿Necesita ayuda de preparación?"
                    if lang == 'es' else
                    f"Urgent reminder: '{summary}' is tomorrow {date_str}. Do you need any preparation help?"
                )

            conv_id = str(row['conversation_id']) if row.get('conversation_id') else ''
            _invoke_send(row['secret_arn'], org_id, conv_id, row['phone'], msg)

            with conn.cursor() as cur:
                cur.execute(
                    f"UPDATE firm_os.clio_calendar_entries SET {flag_col} = TRUE WHERE id = %s",
                    (row['id'],)
                )
            conn.commit()
            log_audit(conn, org_id, 'reminder-scheduler', f'reminder.sent_{hours}h',
                      {'contact_id': str(row['contact_id'])})
            sent += 1
        except Exception as exc:
            logger.error("%dh reminder error row %s: %s", hours, row.get('id'), exc)
            errors += 1

    return sent, errors


def lambda_handler(event, context):
    conn = get_connection()
    now = datetime.now(timezone.utc)

    s48, e48 = _send_reminders(
        conn,
        now + timedelta(hours=23),
        now + timedelta(hours=49),
        'reminder_sent_48h',
        48,
    )
    s24, e24 = _send_reminders(
        conn,
        now + timedelta(hours=0),
        now + timedelta(hours=25),
        'reminder_sent_24h',
        24,
    )

    result = {'sent': s48 + s24, 'errors': e48 + e24}
    logger.info("reminder-scheduler done: %s", result)
    return result
