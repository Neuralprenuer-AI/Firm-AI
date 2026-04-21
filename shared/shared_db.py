import json
import boto3
import psycopg2
from psycopg2.extras import RealDictCursor

_conn = None

def get_connection():
    global _conn
    if _conn is None or _conn.closed:
        client = boto3.client('secretsmanager', region_name='us-east-2')
        secret = json.loads(
            client.get_secret_value(SecretId='firmos/rds/credentials')['SecretString']
        )
        _conn = psycopg2.connect(secret['url'], cursor_factory=RealDictCursor)
    return _conn

def assert_org_access(caller_org_id: str, record_org_id: str):
    if str(caller_org_id) != str(record_org_id):
        raise PermissionError(f"org_id mismatch: {caller_org_id} != {record_org_id}")

def log_audit(conn, org_id: str, actor: str, event_type: str, payload: dict, severity: str = 'info'):
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO firm_os.audit_log (org_id, actor, event_type, severity, payload)
               VALUES (%s, %s, %s, %s, %s)""",
            (org_id, actor, event_type, severity, json.dumps(payload))
        )
    conn.commit()
