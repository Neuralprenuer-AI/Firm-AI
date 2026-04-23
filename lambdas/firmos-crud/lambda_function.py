import json
import os
import sys
import jwt as pyjwt
sys.path.insert(0, '/opt/python')

from shared_auth import auth_context as _jwks_auth_context, get_org_id, get_role
from shared_db import get_connection, assert_org_access, log_audit


def _get_secret():
    if os.environ.get('AWS_LAMBDA_FUNCTION_NAME'):
        return None  # production: always use JWKS
    return None  # tests: patched externally


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
    except PermissionError as e:
        return _resp(401, {'error': str(e)})
    except Exception as e:
        import logging
        logging.getLogger(__name__).error("crud unhandled error: %s", type(e).__name__)
        return _resp(500, {'error': 'internal server error'})

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
        if not org_id:
            return _resp(400, {'error': 'org_id required for super_admin'})
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
        if not org_id:
            return _resp(400, {'error': 'org_id required for super_admin'})
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
        if role == 'firm_admin':
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT org_id FROM firm_os.escalations WHERE escalation_id = %s",
                    (esc_id,)
                )
                esc_row = cur.fetchone()
            if not esc_row:
                return _resp(404, {'error': 'not found'})
            try:
                assert_org_access(caller_org_id, str(esc_row['org_id']))
            except PermissionError:
                return _resp(403, {'error': 'forbidden'})
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
        log_audit(conn, str(row['org_id']), claims.get('sub', 'system'), 'escalation.updated', updates)
        return _resp(200, dict(row)) if row else _resp(404, {'error': 'not found'})

    # GET /firmos/dashboard/stats
    if path == '/firmos/dashboard/stats' and method == 'GET':
        if role == 'super_admin':
            org_id = (event.get('queryStringParameters') or {}).get('org_id')
            if not org_id:
                return _resp(400, {'error': 'org_id required for super_admin'})
        else:
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
        if role not in ('super_admin', 'firm_admin'):
            return _resp(403, {'error': 'forbidden'})
        org_id = caller_org_id
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM firm_os.org_users WHERE org_id = %s ORDER BY created_at", (org_id,))
            rows = cur.fetchall()
        return _resp(200, [dict(r) for r in rows])

    # POST /firmos/team
    if path == '/firmos/team' and method == 'POST':
        if role not in ('super_admin', 'firm_admin'):
            return _resp(403, {'error': 'forbidden'})
        required_team_fields = ['name', 'email']
        missing = [f for f in required_team_fields if not body.get(f)]
        if missing:
            return _resp(400, {'error': f'missing required fields: {missing}'})
        valid_roles = {'partner', 'associate', 'paralegal', 'admin'}
        if body.get('org_role') and body['org_role'] not in valid_roles:
            return _resp(400, {'error': f'invalid org_role, must be one of: {sorted(valid_roles)}'})
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
        log_audit(conn, org_id, claims.get('sub', 'system'), 'team.member_added', {'email': body['email'], 'org_role': body.get('org_role', 'associate')})
        return _resp(201, dict(row))

    # DELETE /firmos/team/{user_id}
    if path.startswith('/firmos/team/') and method == 'DELETE' and params.get('user_id'):
        if role not in ('super_admin', 'firm_admin'):
            return _resp(403, {'error': 'forbidden'})
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM firm_os.org_users WHERE user_id = %s AND org_id = %s RETURNING user_id",
                (params['user_id'], caller_org_id)
            )
            row = cur.fetchone()
        conn.commit()
        if row:
            log_audit(conn, caller_org_id, claims.get('sub', 'system'), 'team.member_removed', {'user_id': params['user_id']})
        return _resp(200, {'deleted': True}) if row else _resp(404, {'error': 'not found'})

    # PATCH /firmos/settings
    if path == '/firmos/settings' and method == 'PATCH':
        if role not in ('super_admin', 'firm_admin'):
            return _resp(403, {'error': 'forbidden'})
        if role == 'super_admin':
            org_id = body.get('org_id') or (event.get('queryStringParameters') or {}).get('org_id')
            if not org_id:
                return _resp(400, {'error': 'org_id required for super_admin'})
        else:
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
        log_audit(conn, org_id, claims.get('sub', 'system'), 'org.settings_updated', updates)
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

    # GET /firmos/settings/firm
    if path == '/firmos/settings/firm' and method == 'GET':
        if role != 'firm_admin':
            return _resp(403, {'error': 'firm_admin required'})
        with conn.cursor() as cur:
            cur.execute(
                "SELECT name, practice_area, agent_display_name, timezone, partner_email, "
                "emergency_contact_number, intake_phone_number, status_phone_number "
                "FROM firm_os.organizations WHERE org_id = %s",
                (caller_org_id,)
            )
            row = cur.fetchone()
        return _resp(200, dict(row)) if row else _resp(404, {'error': 'not found'})

    # PATCH /firmos/settings/firm
    if path == '/firmos/settings/firm' and method == 'PATCH':
        if role != 'firm_admin':
            return _resp(403, {'error': 'firm_admin required'})
        allowed = {'name', 'practice_area', 'agent_display_name', 'timezone', 'partner_email', 'emergency_contact_number'}
        updates = {k: v for k, v in body.items() if k in allowed}
        if not updates:
            return _resp(400, {'error': 'no valid fields'})
        set_clause = ', '.join(f"{k} = %s" for k in updates)
        with conn.cursor() as cur:
            cur.execute(
                f"UPDATE firm_os.organizations SET {set_clause}, updated_at = NOW() "
                f"WHERE org_id = %s RETURNING *",
                list(updates.values()) + [caller_org_id]
            )
            row = cur.fetchone()
        conn.commit()
        log_audit(conn, caller_org_id, claims.get('sub', 'system'), 'org.firm_settings_updated', updates)
        return _resp(200, dict(row)) if row else _resp(404, {'error': 'not found'})

    # GET /firmos/settings/voice
    if path == '/firmos/settings/voice' and method == 'GET':
        if role != 'firm_admin':
            return _resp(403, {'error': 'firm_admin required'})
        with conn.cursor() as cur:
            cur.execute(
                "SELECT agent_display_name, after_hours_only, greeting_message_en, greeting_message_es, "
                "after_hours_start, after_hours_end, timezone "
                "FROM firm_os.organizations WHERE org_id = %s",
                (caller_org_id,)
            )
            row = cur.fetchone()
        return _resp(200, dict(row)) if row else _resp(404, {'error': 'not found'})

    # PATCH /firmos/settings/voice
    if path == '/firmos/settings/voice' and method == 'PATCH':
        if role != 'firm_admin':
            return _resp(403, {'error': 'firm_admin required'})
        allowed = {'agent_display_name', 'after_hours_only', 'greeting_message_en', 'greeting_message_es', 'after_hours_start', 'after_hours_end'}
        updates = {k: v for k, v in body.items() if k in allowed}
        if not updates:
            return _resp(400, {'error': 'no valid fields'})
        set_clause = ', '.join(f"{k} = %s" for k in updates)
        with conn.cursor() as cur:
            cur.execute(
                f"UPDATE firm_os.organizations SET {set_clause}, updated_at = NOW() "
                f"WHERE org_id = %s RETURNING *",
                list(updates.values()) + [caller_org_id]
            )
            row = cur.fetchone()
        conn.commit()
        log_audit(conn, caller_org_id, claims.get('sub', 'system'), 'org.voice_settings_updated', updates)
        return _resp(200, dict(row)) if row else _resp(404, {'error': 'not found'})

    # GET /firmos/settings/compliance
    if path == '/firmos/settings/compliance' and method == 'GET':
        if role != 'firm_admin':
            return _resp(403, {'error': 'firm_admin required'})
        with conn.cursor() as cur:
            cur.execute(
                "SELECT mandatory_disclaimer, escalation_keywords, audit_digest_recipients "
                "FROM firm_os.organizations WHERE org_id = %s",
                (caller_org_id,)
            )
            row = cur.fetchone()
        return _resp(200, dict(row)) if row else _resp(404, {'error': 'not found'})

    # PATCH /firmos/settings/compliance
    if path == '/firmos/settings/compliance' and method == 'PATCH':
        if role != 'firm_admin':
            return _resp(403, {'error': 'firm_admin required'})
        allowed = {'mandatory_disclaimer', 'escalation_keywords'}
        updates = {k: v for k, v in body.items() if k in allowed}
        if 'escalation_keywords' in updates:
            updates['escalation_keywords'] = body.get('escalation_keywords', [])
        if not updates:
            return _resp(400, {'error': 'no valid fields'})
        set_clause = ', '.join(f"{k} = %s" for k in updates)
        with conn.cursor() as cur:
            cur.execute(
                f"UPDATE firm_os.organizations SET {set_clause}, updated_at = NOW() "
                f"WHERE org_id = %s RETURNING *",
                list(updates.values()) + [caller_org_id]
            )
            row = cur.fetchone()
        conn.commit()
        log_audit(conn, caller_org_id, claims.get('sub', 'system'), 'org.compliance_settings_updated', {k: v for k, v in updates.items() if k != 'escalation_keywords'})
        return _resp(200, dict(row)) if row else _resp(404, {'error': 'not found'})

    # GET /firmos/settings/crm
    if path == '/firmos/settings/crm' and method == 'GET':
        if role != 'firm_admin':
            return _resp(403, {'error': 'firm_admin required'})
        with conn.cursor() as cur:
            cur.execute(
                "SELECT crm_platform, clio_access_token, clio_token_expires_at, updated_at "
                "FROM firm_os.organizations WHERE org_id = %s",
                (caller_org_id,)
            )
            row = cur.fetchone()
        if not row:
            return _resp(404, {'error': 'not found'})
        r = dict(row)
        connected = bool(r.get('crm_platform') and r.get('clio_access_token'))
        return _resp(200, {
            'connected': connected,
            'platform': r.get('crm_platform'),
            'connected_at': r.get('clio_token_expires_at') or r.get('updated_at')
        })

    # DELETE /firmos/settings/crm
    if path == '/firmos/settings/crm' and method == 'DELETE':
        if role != 'firm_admin':
            return _resp(403, {'error': 'firm_admin required'})
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE firm_os.organizations SET crm_platform = NULL, clio_access_token = NULL, "
                "clio_refresh_token = NULL, clio_token_expires_at = NULL, updated_at = NOW() "
                "WHERE org_id = %s",
                (caller_org_id,)
            )
        conn.commit()
        log_audit(conn, caller_org_id, claims.get('sub', 'system'), 'org.crm_disconnected', {})
        return _resp(200, {'disconnected': True})

    # GET /firmos/settings/crm/oauth-url
    if path == '/firmos/settings/crm/oauth-url' and method == 'GET':
        if role != 'firm_admin':
            return _resp(403, {'error': 'firm_admin required'})
        return _resp(200, {
            'url': 'https://app.clio.com/oauth/authorize?response_type=code&client_id=placeholder&redirect_uri=https://app.neuralpreneur.com/settings/crm/callback&scope=contacts%3Aread%20matters%3Aread'
        })

    # GET /firmos/contacts/{contact_id}/conversations
    if path.startswith('/firmos/contacts/') and method == 'GET':
        parts = path.split('/')
        contact_id = parts[3] if len(parts) >= 5 and parts[4] == 'conversations' else None
        if contact_id:
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
                    "SELECT * FROM firm_os.conversations WHERE contact_id = %s ORDER BY created_at DESC",
                    (contact_id,)
                )
                rows = cur.fetchall()
            return _resp(200, [dict(r) for r in rows])
        # GET /firmos/contacts/{contact_id} — single contact
        single_contact_id = params.get('contact_id') or (parts[3] if len(parts) >= 4 else None)
        if single_contact_id:
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM firm_os.contacts WHERE contact_id = %s", (single_contact_id,))
                row = cur.fetchone()
            if not row:
                return _resp(404, {'error': 'not found'})
            if role == 'firm_admin':
                try:
                    assert_org_access(caller_org_id, str(row['org_id']))
                except PermissionError:
                    return _resp(403, {'error': 'forbidden'})
            return _resp(200, dict(row))

    # GET /firmos/conversations/{id}/messages
    if path.endswith('/messages') and method == 'GET':
        parts = path.split('/')
        conv_id = parts[3] if len(parts) >= 5 else None
        if conv_id:
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM firm_os.messages WHERE conversation_id = %s ORDER BY created_at ASC", (conv_id,))
                rows = cur.fetchall()
            return _resp(200, [dict(r) for r in rows])

    # GET /firmos/conversations/{id} — single conversation
    if path.startswith('/firmos/conversations/') and method == 'GET' and params.get('id') and not path.endswith('/messages'):
        conv_id = params['id']
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM firm_os.conversations WHERE conversation_id = %s", (conv_id,))
            row = cur.fetchone()
        if not row:
            return _resp(404, {'error': 'not found'})
        if role == 'firm_admin':
            try:
                assert_org_access(caller_org_id, str(row['org_id']))
            except PermissionError:
                return _resp(403, {'error': 'forbidden'})
        return _resp(200, dict(row))

    # GET /firmos/intakes
    if path == '/firmos/intakes' and method == 'GET':
        if role != 'firm_admin':
            return _resp(403, {'error': 'firm_admin required'})
        with conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM firm_os.intake_records WHERE org_id = %s ORDER BY created_at DESC",
                (caller_org_id,)
            )
            rows = cur.fetchall()
        return _resp(200, [dict(r) for r in rows])

    # GET /firmos/calls
    if path == '/firmos/calls' and method == 'GET':
        return _resp(200, [])

    return _resp(404, {'error': 'route not found'})
