# shared/shared_auth.py
import json
import boto3
import jwt
from jwt import PyJWKClient

_jwks_client = None

def _get_jwks_client() -> PyJWKClient:
    global _jwks_client
    if _jwks_client is None:
        sm = boto3.client('secretsmanager', region_name='us-east-2')
        raw = sm.get_secret_value(SecretId='firmos/supabase/jwks-url')['SecretString']
        url = json.loads(raw)['url']
        _jwks_client = PyJWKClient(url)
    return _jwks_client

def verify_jwt(token: str) -> dict:
    try:
        client = _get_jwks_client()
        signing_key = client.get_signing_key_from_jwt(token)
        return jwt.decode(token, signing_key.key, algorithms=['RS256'])
    except jwt.PyJWTError as e:
        raise PermissionError(f"Invalid token: {type(e).__name__}") from e

def get_org_id(claims: dict) -> str:
    return claims.get('app_metadata', {}).get('org_id')

def get_role(claims: dict) -> str:
    return claims.get('app_metadata', {}).get('role')

def require_role(claims: dict, required_role: str):
    role = get_role(claims)
    if role != required_role:
        raise PermissionError(f"Required role {required_role}, got {role}")

def auth_context(event: dict) -> dict:
    auth_header = event.get('headers', {}).get('Authorization', '')
    if not auth_header.startswith('Bearer '):
        raise PermissionError("Missing Authorization header")
    token = auth_header[7:]
    return verify_jwt(token)
