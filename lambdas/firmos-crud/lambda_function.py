import json
import sys
import jwt as pyjwt
sys.path.insert(0, '/opt/python')

from shared_auth import auth_context as _jwks_auth_context, get_org_id, get_role
from shared_db import get_connection, assert_org_access, log_audit


def _get_secret():
    return None  # In production: returns None, falls through to JWKS


def _auth(event):
    secret = _get_secret()
    if secret:
        auth_header = event.get('headers', {}).get('Authorization', '')
        if not auth_header.startswith('Bearer '):
            raise PermissionError("Missing Authorization header")
        token = auth_header[7:]
        return pyjwt.decode(token, secret, algorithms=['HS256'])
    return _jwks_auth_context(event)


def _resp(status, body):
    return {
        'statusCode': status,
        'headers': {'Content-Type': 'application/json', 'Access-Control-Allow-Origin': '*'},
        'body': json.dumps(body, default=str)
    }


def lambda_handler(event, context):
    try:
        claims = _auth(event)
    except (PermissionError, Exception) as e:
        return _resp(401, {'error': str(e)})

    role = get_role(claims)
    caller_org_id = get_org_id(claims)
    path = event.get('path', '')
    method = event.get('httpMethod', 'GET')
    params = event.get('pathParameters') or {}
    body = json.loads(event.get('body') or '{}')
    conn = get_connection()

    # GET /firmos/firms — super admin only
    if path == '/firmos/firms' and method == 'GET':
        if role != 'super_admin':
            return _resp(403, {'error': 'super_admin required'})
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM firm_os.organizations ORDER BY created_at DESC")
            rows = cur.fetchall()
        return _resp(200, [dict(r) for r in rows])

    # GET /firmos/firms/{org_id}
    if path.startswith('/firmos/firms/') and method == 'GET' and params.get('org_id'):
        target = params['org_id']
        if role == 'firm_admin':
            try:
                assert_org_access(caller_org_id, target)
            except PermissionError:
                return _resp(403, {'error': 'forbidden'})
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM firm_os.organizations WHERE org_id = %s", (target,))
            row = cur.fetchone()
        return _resp(200, dict(row)) if row else _resp(404, {'error': 'not found'})

    # PATCH /firmos/firms/{org_id}
    if path.startswith('/firmos/firms/') and method == 'PATCH' and params.get('org_id'):
        if role != 'super_admin':
            return _resp(403, {'error': 'super_admin required'})
        target = params['org_id']
        allowed = {'name', 'billing_status', 'status', 'practice_area', 'monthly_sms_budget'}
        updates = {k: v for k, v in body.items() if k in allowed}
        if not updates:
            return _resp(400, {'error': 'no valid fields'})
        set_clause = ', '.join(f"{k} = %s" for k in updates)
        with conn.cursor() as cur:
            cur.execute(
                f"UPDATE firm_os.organizations SET {set_clause}, updated_at = NOW() "
                f"WHERE org_id = %s RETURNING *",
                list(updates.values()) + [target]
            )
            row = cur.fetchone()
        conn.commit()
        log_audit(conn, target, claims.get('sub', 'system'), 'org.updated', updates)
        return _resp(200, dict(row)) if row else _resp(404, {'error': 'not found'})

    # GET /firmos/contacts
    if path == '/firmos/contacts' and method == 'GET':
        if role not in ('super_admin', 'firm_admin'):
            return _resp(403, {'error': 'forbidden'})
        org_id = caller_org_id if role == 'firm_admin' else (event.get('queryStringParameters') or {}).get('org_id')
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM firm_os.contacts WHERE org_id = %s ORDER BY created_at DESC", (org_id,))
            rows = cur.fetchall()
        return _resp(200, [dict(r) for r in rows])

    # GET /firmos/conversations
    if path == '/firmos/conversations' and method == 'GET':
        contact_id = (event.get('queryStringParameters') or {}).get('contact_id')
        if not contact_id:
            return _resp(400, {'error': 'contact_id required'})
        with conn.cursor() as cur:
            cur.execute("SELECT org_id FROM firm_os.contacts WHERE contact_id = %s", (contact_id,))
            c = cur.fetchone()
        if not c:
            return _resp(404, {'error': 'contact not found'})
        if role == 'firm_admin':
            try:
                assert_org_access(caller_org_id, str(c['org_id']))
            except PermissionError:
                return _resp(403, {'error': 'forbidden'})
        with conn.cursor() as cur:
            cur.execute(
                "SELECT m.* FROM firm_os.messages m "
                "JOIN firm_os.conversations cv ON m.conversation_id = cv.conversation_id "
                "WHERE cv.contact_id = %s ORDER BY m.created_at ASC",
                (contact_id,)
            )
            rows = cur.fetchall()
        return _resp(200, [dict(r) for r in rows])

    # GET /firmos/escalations
    if path == '/firmos/escalations' and method == 'GET':
        org_id = caller_org_id if role == 'firm_admin' else (event.get('queryStringParameters') or {}).get('org_id')
        status_filter = (event.get('queryStringParameters') or {}).get('status', 'open')
        with conn.cursor() as cur:
            cur.execute(
                "SELECT e.*, c.phone, c.name as contact_name FROM firm_os.escalations e "
                "JOIN firm_os.contacts c ON e.contact_id = c.contact_id "
                "WHERE e.org_id = %s AND e.status = %s ORDER BY e.created_at DESC",
                (org_id, status_filter)
            )
            rows = cur.fetchall()
        return _resp(200, [dict(r) for r in rows])

    # PATCH /firmos/escalations/{id}
    if path.startswith('/firmos/escalations/') and method == 'PATCH' and params.get('id'):
        esc_id = params['id']
        allowed = {'status', 'assigned_user_id'}
        updates = {k: v for k, v in body.items() if k in allowed}
        if not updates:
            return _resp(400, {'error': 'no valid fields'})
        set_clause = ', '.join(f"{k} = %s" for k in updates)
        extra_sql = ", resolved_at = NOW()" if updates.get('status') == 'resolved' else ""
        with conn.cursor() as cur:
            cur.execute(
                f"UPDATE firm_os.escalations SET {set_clause}{extra_sql} "
                f"WHERE escalation_id = %s RETURNING *",
                list(updates.values()) + [esc_id]
            )
            row = cur.fetchone()
        conn.commit()
        return _resp(200, dict(row)) if row else _resp(404, {'error': 'not found'})

    # GET /firmos/dashboard/stats
    if path == '/firmos/dashboard/stats' and method == 'GET':
        org_id = caller_org_id
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM firm_os.messages WHERE org_id = %s AND direction = 'outbound' AND created_at >= CURRENT_DATE",
                (org_id,)
            )
            sms_today = cur.fetchone()['count']
            cur.execute(
                "SELECT COUNT(*) FROM firm_os.conversations WHERE org_id = %s AND state = 'intake_in_progress'",
                (org_id,)
            )
            active_convs = cur.fetchone()['count']
            cur.execute(
                "SELECT COUNT(*) FROM firm_os.escalations WHERE org_id = %s AND status = 'open'",
                (org_id,)
            )
            open_escs = cur.fetchone()['count']
            cur.execute(
                "SELECT COUNT(*) FROM firm_os.contacts WHERE org_id = %s AND created_at >= NOW() - INTERVAL '7 days'",
                (org_id,)
            )
            new_contacts = cur.fetchone()['count']
            cur.execute(
                "SELECT DATE(created_at) as day, COUNT(*) FROM firm_os.messages "
                "WHERE org_id = %s AND created_at >= NOW() - INTERVAL '7 days' "
                "GROUP BY day ORDER BY day",
                (org_id,)
            )
            chart = [dict(r) for r in cur.fetchall()]
        return _resp(200, {
            'sms_today': sms_today,
            'active_conversations': active_convs,
            'open_escalations': open_escs,
            'new_contacts_week': new_contacts,
            'volume_chart': chart
        })

    # GET /firmos/team
    if path == '/firmos/team' and method == 'GET':
        org_id = caller_org_id
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM firm_os.org_users WHERE org_id = %s ORDER BY created_at", (org_id,))
            rows = cur.fetchall()
        return _resp(200, [dict(r) for r in rows])

    # POST /firmos/team
    if path == '/firmos/team' and method == 'POST':
        org_id = caller_org_id
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO firm_os.org_users (org_id, name, email, org_role, escalation_routing) "
                "VALUES (%s, %s, %s, %s, %s) RETURNING *",
                (org_id, body['name'], body['email'],
                 body.get('org_role', 'associate'), body.get('escalation_routing', False))
            )
            row = cur.fetchone()
        conn.commit()
        return _resp(201, dict(row))

    # DELETE /firmos/team/{user_id}
    if path.startswith('/firmos/team/') and method == 'DELETE' and params.get('user_id'):
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM firm_os.org_users WHERE user_id = %s AND org_id = %s RETURNING user_id",
                (params['user_id'], caller_org_id)
            )
            row = cur.fetchone()
        conn.commit()
        return _resp(200, {'deleted': True}) if row else _resp(404, {'error': 'not found'})

    # PATCH /firmos/settings
    if path == '/firmos/settings' and method == 'PATCH':
        org_id = caller_org_id
        allowed = {'monthly_sms_budget', 'after_hours_en', 'after_hours_es', 'timezone', 'default_language'}
        updates = {k: v for k, v in body.items() if k in allowed}
        if not updates:
            return _resp(400, {'error': 'no valid fields'})
        set_clause = ', '.join(f"{k} = %s" for k in updates)
        with conn.cursor() as cur:
            cur.execute(
                f"UPDATE firm_os.organizations SET {set_clause}, updated_at = NOW() "
                f"WHERE org_id = %s RETURNING *",
                list(updates.values()) + [org_id]
            )
            row = cur.fetchone()
        conn.commit()
        return _resp(200, dict(row))

    # GET /firmos/audit
    if path == '/firmos/audit' and method == 'GET':
        if role != 'super_admin':
            return _resp(403, {'error': 'super_admin required'})
        qp = event.get('queryStringParameters') or {}
        org_filter = qp.get('org_id')
        sev_filter = qp.get('severity')
        conditions, vals = [], []
        if org_filter:
            conditions.append("org_id = %s")
            vals.append(org_filter)
        if sev_filter:
            conditions.append("severity = %s")
            vals.append(sev_filter)
        where = ('WHERE ' + ' AND '.join(conditions)) if conditions else ''
        with conn.cursor() as cur:
            cur.execute(f"SELECT * FROM firm_os.audit_log {where} ORDER BY created_at DESC LIMIT 500", vals)
            rows = cur.fetchall()
        return _resp(200, [dict(r) for r in rows])

    return _resp(404, {'error': 'route not found'})
