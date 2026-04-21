import json
import sys
sys.path.insert(0, '/opt/python')

from shared_auth import auth_context, get_org_id, get_role
from shared_db import get_connection

def lambda_handler(event, context):
    try:
        claims = auth_context(event)
    except PermissionError as e:
        return {'statusCode': 401, 'body': json.dumps({'error': str(e)})}

    org_id = get_org_id(claims)
    role = get_role(claims)
    conn = get_connection()

    org = None
    if org_id:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT org_id, name, practice_area, billing_status, status "
                "FROM firm_os.organizations WHERE org_id = %s",
                (org_id,)
            )
            org = cur.fetchone()

    return {
        'statusCode': 200,
        'headers': {'Content-Type': 'application/json'},
        'body': json.dumps({
            'user_id': claims.get('sub'),
            'role': role,
            'org_id': org_id,
            'org': dict(org) if org else None
        })
    }
