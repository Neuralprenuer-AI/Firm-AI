import json
import requests
import sys
sys.path.insert(0, '/opt/python')

from shared_db import get_connection, log_audit

CLIO_API = 'https://app.clio.com/api/v4'

def lambda_handler(event, context):
    org_id = event['org_id']
    contact_id = event['contact_id']
    intake_id = event['intake_id']
    conversation_text = event['conversation_text']

    conn = get_connection()

    with conn.cursor() as cur:
        cur.execute("SELECT * FROM firm_os.organizations WHERE org_id = %s", (org_id,))
        org = cur.fetchone()

    with conn.cursor() as cur:
        cur.execute("SELECT * FROM firm_os.contacts WHERE contact_id = %s", (contact_id,))
        contact = cur.fetchone()

    with conn.cursor() as cur:
        cur.execute("SELECT * FROM firm_os.intake_records WHERE intake_id = %s", (intake_id,))
        intake = cur.fetchone()

    token = org['clio_access_token']
    headers = {'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'}

    clio_contact_id = contact.get('clio_contact_id')
    if not clio_contact_id:
        contact_name = contact.get('name') or contact['phone']
        resp = requests.post(f'{CLIO_API}/contacts', headers=headers, json={
            'data': {'name': contact_name, 'phone_numbers': [{'name': 'Mobile', 'number': contact['phone']}]}
        })
        if resp.status_code in (200, 201):
            clio_contact_id = resp.json()['data']['id']
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE firm_os.contacts SET clio_contact_id = %s WHERE contact_id = %s",
                    (str(clio_contact_id), contact_id)
                )
            conn.commit()

    note_subject = f"SMS Intake — {org['practice_area'].replace('_', ' ').title()}"
    note_body = f"Intake collected via SMS:\n\n{conversation_text}"

    note_payload = {
        'data': {
            'subject': note_subject,
            'detail': note_body,
            'contact': {'id': clio_contact_id} if clio_contact_id else None
        }
    }
    note_resp = requests.post(f'{CLIO_API}/notes', headers=headers, json=note_payload)

    if note_resp.status_code in (200, 201):
        note_id = note_resp.json()['data']['id']
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE firm_os.intake_records SET clio_note_id = %s WHERE intake_id = %s",
                (str(note_id), intake_id)
            )
        conn.commit()
        log_audit(conn, org_id, 'clio-sync', 'clio.note_created',
                  {'intake_id': intake_id, 'note_id': str(note_id)})
    else:
        log_audit(conn, org_id, 'clio-sync', 'clio.note_failed',
                  {'intake_id': intake_id, 'status': note_resp.status_code}, 'warning')
