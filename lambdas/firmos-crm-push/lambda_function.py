import json
import boto3
import requests
import sys
import logging
from datetime import datetime, timezone

sys.path.insert(0, '/opt/python')
from shared_db import get_connection, log_audit

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

CLIO_API = 'https://app.clio.com/api/v4'
CLIO_REQUEST_TIMEOUT = 10


# ---------------------------------------------------------------------------
# Token validation
# ---------------------------------------------------------------------------

def _token_is_valid(org: dict) -> bool:
    """Return True only when the org has a non-expired Clio access token."""
    if not org.get('clio_access_token'):
        return False
    expires_at = org.get('clio_token_expires_at')
    if expires_at is None:
        return False
    now = datetime.now(timezone.utc)
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    return now < expires_at


# ---------------------------------------------------------------------------
# Clio API helpers
# ---------------------------------------------------------------------------

def _clio_headers(token: str) -> dict:
    return {'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'}


def _create_clio_contact(token: str, name: str, phone: str) -> tuple[int | None, str | None]:
    """
    POST /api/v4/contacts.

    Returns (clio_id, None) on success or (None, error_body) on 4xx/5xx.
    """
    payload = {
        'data': {
            'name': name,
            'type': 'Person',
            'phone_numbers': [
                {'name': 'Mobile', 'number': phone, 'default_number': True}
            ],
        }
    }
    try:
        resp = requests.post(
            f'{CLIO_API}/contacts',
            headers=_clio_headers(token),
            json=payload,
            timeout=CLIO_REQUEST_TIMEOUT,
        )
    except requests.RequestException as exc:
        return None, str(exc)

    if resp.status_code in (200, 201):
        return resp.json()['data']['id'], None
    return None, resp.text[:500]


def _create_clio_matter(
    token: str,
    clio_contact_id: int,
    description: str,
    practice_area_name: str,
) -> tuple[int | None, str | None]:
    """
    POST /api/v4/matters.

    Returns (matter_id, None) on success or (None, error_body) on 4xx/5xx.
    """
    payload = {
        'data': {
            'client': {'id': clio_contact_id},
            'description': description,
            'practice_area': {'name': practice_area_name},
            'status': 'Pending',
        }
    }
    try:
        resp = requests.post(
            f'{CLIO_API}/matters',
            headers=_clio_headers(token),
            json=payload,
            timeout=CLIO_REQUEST_TIMEOUT,
        )
    except requests.RequestException as exc:
        return None, str(exc)

    if resp.status_code in (200, 201):
        return resp.json()['data']['id'], None
    return None, resp.text[:500]


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def lambda_handler(event, context):
    required_fields = ('org_id', 'contact_id', 'intake_id')
    for field in required_fields:
        if not event.get(field):
            logger.error("crm-push: missing required field=%s", field)
            return {
                'statusCode': 400,
                'error': 'missing_required_field',
                'field': field,
            }

    org_id = str(event['org_id'])
    contact_id = str(event['contact_id'])
    intake_id = str(event['intake_id'])

    conn = get_connection()

    # -----------------------------------------------------------------------
    # 1. Load and validate org
    # -----------------------------------------------------------------------
    with conn.cursor() as cur:
        cur.execute(
            """SELECT org_id, name, crm_platform, practice_area,
                      clio_access_token, clio_token_expires_at
               FROM firm_os.organizations
               WHERE org_id = %s AND status = 'active'""",
            (org_id,),
        )
        org = cur.fetchone()

    if not org:
        logger.warning("crm-push: org not found or inactive — org_id=%s", org_id)
        return {'skipped': True, 'reason': 'org_not_found'}

    if org.get('crm_platform') != 'clio':
        logger.info("crm-push: org %s crm_platform=%s, not using Clio — skipping",
                    org_id, org.get('crm_platform'))
        return {'skipped': True, 'reason': 'crm_platform_not_clio'}

    if not _token_is_valid(org):
        logger.warning("crm-push: org %s has no valid Clio token — skipping", org_id)
        return {'skipped': True, 'reason': 'token_invalid'}

    token = org['clio_access_token']

    # -----------------------------------------------------------------------
    # 2. Load contact (org-scoped)
    # -----------------------------------------------------------------------
    with conn.cursor() as cur:
        cur.execute(
            """SELECT contact_id, org_id, phone, name, clio_contact_id
               FROM firm_os.contacts
               WHERE contact_id = %s AND org_id = %s""",
            (contact_id, org_id),
        )
        contact = cur.fetchone()

    if not contact:
        logger.warning("crm-push: contact not found — contact_id=%s org_id=%s", contact_id, org_id)
        return {'skipped': True, 'reason': 'contact_not_found'}

    # -----------------------------------------------------------------------
    # 3. Load intake record (org-scoped)
    # -----------------------------------------------------------------------
    with conn.cursor() as cur:
        cur.execute(
            """SELECT intake_id, org_id, data, fields, full_name, brief_description,
                      crm_pushed, crm_matter_id
               FROM firm_os.intake_records
               WHERE intake_id = %s AND org_id = %s""",
            (intake_id, org_id),
        )
        intake = cur.fetchone()

    if not intake:
        logger.warning("crm-push: intake not found — intake_id=%s org_id=%s", intake_id, org_id)
        return {'skipped': True, 'reason': 'intake_not_found'}

    # -----------------------------------------------------------------------
    # 4. Idempotency guard — already pushed
    # -----------------------------------------------------------------------
    if intake.get('crm_pushed'):
        logger.info("crm-push: intake %s already pushed — skipping (idempotent)", intake_id)
        return {'skipped': True, 'reason': 'already_pushed', 'crm_matter_id': intake.get('crm_matter_id')}

    # -----------------------------------------------------------------------
    # 5. Extract intake fields from JSONB data
    # -----------------------------------------------------------------------
    # New schema (migration 017): full_name + brief_description as dedicated columns
    # Fall back to old data JSONB for backwards compatibility
    intake_data: dict = intake.get('data') or {}
    fields_data: dict = intake.get('fields') or {}
    intake_name = (
        intake.get('full_name')
        or fields_data.get('full_name')
        or intake_data.get('intake_name')
        or contact.get('name')
        or contact['phone']
    )
    intake_issue = (
        intake.get('brief_description')
        or fields_data.get('brief_description')
        or intake_data.get('intake_issue')
        or 'New matter from SMS intake'
    )
    practice_area_name = (org.get('practice_area') or 'General').replace('_', ' ').title()

    # -----------------------------------------------------------------------
    # 6. Create Clio contact if needed
    # -----------------------------------------------------------------------
    clio_contact_id = contact.get('clio_contact_id')

    if not clio_contact_id:
        contact_name = intake_name
        clio_contact_id, err = _create_clio_contact(token, contact_name, contact['phone'])

        if err:
            logger.error("crm-push: Clio contact creation failed — org=%s err=%s", org_id, err)
            with conn.cursor() as cur:
                cur.execute(
                    """UPDATE firm_os.intake_records
                       SET crm_push_error = %s
                       WHERE intake_id = %s AND org_id = %s""",
                    (f"contact_create: {err}"[:500], intake_id, org_id),
                )
            conn.commit()
            log_audit(conn, org_id, 'crm-push', 'system.crm_push_failed',
                      {'intake_id': intake_id, 'step': 'contact_create', 'error': err},
                      severity='warning')
            return {'pushed': False, 'reason': 'contact_create_failed'}

        with conn.cursor() as cur:
            cur.execute(
                """UPDATE firm_os.contacts
                   SET clio_contact_id = %s
                   WHERE contact_id = %s AND org_id = %s""",
                (str(clio_contact_id), contact_id, org_id),
            )
        conn.commit()
        logger.info("crm-push: created Clio contact %s for contact %s", clio_contact_id, contact_id)

    # -----------------------------------------------------------------------
    # 7. Create Clio matter
    # -----------------------------------------------------------------------
    matter_id, err = _create_clio_matter(token, clio_contact_id, intake_issue, practice_area_name)

    if err:
        logger.error("crm-push: Clio matter creation failed — org=%s err=%s", org_id, err)
        with conn.cursor() as cur:
            cur.execute(
                """UPDATE firm_os.intake_records
                   SET crm_push_error = %s
                   WHERE intake_id = %s AND org_id = %s""",
                (f"matter_create: {err}"[:500], intake_id, org_id),
            )
        conn.commit()
        log_audit(conn, org_id, 'crm-push', 'system.crm_push_failed',
                  {'intake_id': intake_id, 'step': 'matter_create', 'error': err},
                  severity='warning')
        return {'pushed': False, 'reason': 'matter_create_failed'}

    logger.info("crm-push: created Clio matter %s for intake %s", matter_id, intake_id)

    # -----------------------------------------------------------------------
    # 8. Mark intake as pushed
    # -----------------------------------------------------------------------
    with conn.cursor() as cur:
        cur.execute(
            """UPDATE firm_os.intake_records
               SET crm_pushed    = TRUE,
                   crm_pushed_at = NOW(),
                   crm_matter_id = %s,
                   crm_push_error = NULL
               WHERE intake_id = %s AND org_id = %s""",
            (str(matter_id), intake_id, org_id),
        )
    conn.commit()

    # -----------------------------------------------------------------------
    # 9. Audit log
    # -----------------------------------------------------------------------
    log_audit(conn, org_id, 'crm-push', 'system.crm_push_success',
              {
                  'intake_id': intake_id,
                  'contact_id': contact_id,
                  'clio_contact_id': str(clio_contact_id),
                  'crm_matter_id': str(matter_id),
              })

    return {
        'pushed': True,
        'intake_id': intake_id,
        'clio_contact_id': str(clio_contact_id),
        'crm_matter_id': str(matter_id),
    }
