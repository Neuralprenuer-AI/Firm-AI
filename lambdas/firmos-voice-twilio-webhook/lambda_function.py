# lambdas/firmos-voice-twilio-webhook/lambda_function.py
import base64
import hashlib
import hmac
import json
import logging
import sys
import urllib.parse

import boto3
import requests

sys.path.insert(0, '/opt/python')
from shared_db import get_connection

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

REGION = 'us-east-2'
ELEVENLABS_API = 'https://api.elevenlabs.io/v1'
VOICE_WEBHOOK_URL = 'https://kezjhodcig.execute-api.us-east-2.amazonaws.com/prod/firmos/voice/call'
TIMEOUT = 10

_secret_cache: dict = {}


def _get_secret(secret_id: str) -> dict:
    if secret_id not in _secret_cache:
        raw = boto3.client('secretsmanager', region_name=REGION)\
            .get_secret_value(SecretId=secret_id)['SecretString']
        _secret_cache[secret_id] = json.loads(raw)
    return _secret_cache[secret_id]


def _verify_twilio_signature(auth_token: str, url: str, params: dict, signature: str) -> bool:
    sorted_str = ''.join(f'{k}{v}' for k, v in sorted(params.items()))
    s = (url + sorted_str).encode()
    mac = hmac.new(auth_token.encode(), s, hashlib.sha1)
    expected = base64.b64encode(mac.digest()).decode()
    return hmac.compare_digest(expected, signature)


def _twiml_error() -> dict:
    return {
        'statusCode': 200,
        'headers': {'Content-Type': 'application/xml'},
        'body': (
            '<?xml version="1.0" encoding="UTF-8"?><Response>'
            '<Say>We are experiencing technical difficulties. Please call back later.</Say>'
            '</Response>'
        ),
    }


def lambda_handler(event, context):
    body_str = event.get('body') or ''
    if event.get('isBase64Encoded'):
        body_str = base64.b64decode(body_str).decode()

    params = dict(urllib.parse.parse_qsl(body_str))
    from_number = params.get('From', '')
    to_number = params.get('To', '')

    if not from_number or not to_number:
        return _twiml_error()

    conn = get_connection()

    with conn.cursor() as cur:
        cur.execute(
            "SELECT org_id, name, elevenlabs_agent_id, secret_arn "
            "FROM firm_os.organizations "
            "WHERE twilio_phone_number = %s AND status = 'active'",
            (to_number,),
        )
        org = cur.fetchone()

    if not org or not org.get('elevenlabs_agent_id'):
        logger.warning("No active org or agent for number %s", to_number)
        return _twiml_error()

    org_id = str(org['org_id'])

    try:
        secret = _get_secret(org['secret_arn'])
        auth_token = secret['twilio_auth_token']
        signature = (event.get('headers') or {}).get('X-Twilio-Signature', '')
        if not _verify_twilio_signature(auth_token, VOICE_WEBHOOK_URL, params, signature):
            logger.warning("Invalid Twilio signature org=%s", org_id)
            return {'statusCode': 403, 'headers': {'Content-Type': 'text/plain'}, 'body': 'Forbidden'}
    except Exception as exc:
        logger.error("Signature check error org=%s: %s", org_id, exc)
        return _twiml_error()

    try:
        el = _get_secret('firmos/elevenlabs/api-key')
        resp = requests.post(
            f'{ELEVENLABS_API}/conversational_ai/twilio/register_call',
            headers={'xi-api-key': el['api_key'], 'Content-Type': 'application/json'},
            json={
                'agent_id': org['elevenlabs_agent_id'],
                'from_number': from_number,
                'to_number': to_number,
                'direction': 'inbound',
                'conversation_initiation_client_data': {
                    'custom_llm_extra_body': {'org_id': org_id}
                },
            },
            timeout=TIMEOUT,
        )
        if resp.status_code not in (200, 201):
            logger.error("ElevenLabs register_call %s: %s", resp.status_code, resp.text[:300])
            return _twiml_error()

        logger.info("Call registered org=%s from=%s", org_id, from_number)
        return {
            'statusCode': 200,
            'headers': {'Content-Type': 'application/xml'},
            'body': resp.text,
        }
    except Exception as exc:
        logger.error("ElevenLabs register_call exception org=%s: %s", org_id, exc)
        return _twiml_error()
