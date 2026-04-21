import json
import logging
import requests
import sys
sys.path.insert(0, '/opt/python')

from shared_db import get_connection, log_audit

logger = logging.getLogger(__name__)

CLIO_API = 'https://app.clio.com/api/v4'

def lambda_handler(event, context):
    org_id = event['org_id']
    contact_id = event['contact_id']
    intake_id = event['intake_id']
    conversation_text = event['conversation_text']

    try:
        conn = get_connection()

        # 1. Get org
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM firm_os.organizations WHERE org_id = %s", (org_id,))
            org = cur.fetchone()
        if not org:
            raise ValueError(f"org not found: {org_id}")

        # 2. Get contact (org-scoped)
        with conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM firm_os.contacts WHERE contact_id = %s AND org_id = %s",
                (contact_id, org_id)
            )
            contact = cur.fetchone()
        if not contact:
            raise ValueError(f"contact not found: {contact_id} for org: {org_id}")

        # 3. Get intake (org-scoped)
        with conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM firm_os.intake_records WHERE intake_id = %s AND org_id = %s",
                (intake_id, org_id)
            )
            intake = cur.fetchone()
        if not intake:
            raise ValueError(f"intake not found: {intake_id} for org: {org_id}")

        # 4. Idempotency check — already synced, nothing to do
        if intake.get('clio_note_id'):
            return

        token = org['clio_access_token']
        headers = {'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'}

        clio_contact_id = contact.get('clio_contact_id')
        if not clio_contact_id:
            contact_name = contact.get('name') or contact['phone']
            resp = requests.post(f'{CLIO_API}/contacts', headers=headers, json={
                'data': {'name': contact_name, 'phone_numbers': [{'name': 'Mobile', 'number': contact['phone']}]}
            }, timeout=10)
            if resp.status_code in (200, 201):
                clio_contact_id = resp.json()['data']['id']
                with conn.cursor() as cur:
                    cur.execute(
                        "UPDATE firm_os.contacts SET clio_contact_id = %s WHERE contact_id = %s AND org_id = %s",
                        (str(clio_contact_id), contact_id, org_id)
                    )
                conn.commit()
            else:
                log_audit(conn, org_id, 'clio-sync', 'clio.contact_create_failed',
                          {'contact_id': contact_id, 'status': resp.status_code}, 'warning')

        # Fix 4: guard practice_area None
        note_subject = f"SMS Intake — {(org.get('practice_area') or 'General').replace('_', ' ').title()}"
        note_body = f"Intake collected via SMS:\n\n{conversation_text}"

        note_payload = {
            'data': {
                'subject': note_subject,
                'detail': note_body,
                'contact': {'id': clio_contact_id} if clio_contact_id else None
            }
        }
        note_resp = requests.post(f'{CLIO_API}/notes', headers=headers, json=note_payload, timeout=10)

        if note_resp.status_code in (200, 201):
            note_id = note_resp.json()['data']['id']
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE firm_os.intake_records SET clio_note_id = %s WHERE intake_id = %s AND org_id = %s",
                    (str(note_id), intake_id, org_id)
                )
            conn.commit()
            log_audit(conn, org_id, 'clio-sync', 'clio.note_created',
                      {'intake_id': intake_id, 'note_id': str(note_id)})
        else:
            log_audit(conn, org_id, 'clio-sync', 'clio.note_failed',
                      {'intake_id': intake_id, 'status': note_resp.status_code}, 'warning')

    except Exception as e:
        logger.error("clio-sync error: %s | org_id=%s intake_id=%s", type(e).__name__, org_id, intake_id)
        raise
