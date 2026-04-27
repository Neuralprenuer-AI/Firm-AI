# lambdas/firmos-vapi-webhook/lambda_function.py
import hashlib
import hmac
import json
import logging
import re
import sys
import uuid
from datetime import datetime, timezone

import boto3

sys.path.insert(0, '/opt/python')
from shared_db import get_connection, log_audit

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

REGION = 'us-east-2'
EMERGENCY_PATTERNS = [re.compile(p) for p in [
    r'\bemergency\b', r'\barrested\b', r'\bice\b', r'\binjured\b',
    r'\bdying\b', r'\burgent\b',
    r'\bemergencia\b', r'\barrestado\b', r'\bdetenido\b', r'\bherido\b',
]]

_secret_cache: dict = {}


def _get_secret(secret_id: str) -> dict:
    if secret_id not in _secret_cache:
        raw = boto3.client('secretsmanager', region_name=REGION)\
            .get_secret_value(SecretId=secret_id)['SecretString']
        _secret_cache[secret_id] = json.loads(raw)
    return _secret_cache[secret_id]


def _resp(status: int, body: dict) -> dict:
    return {
        'statusCode': status,
        'headers': {'Content-Type': 'application/json'},
        'body': json.dumps(body, default=str),
    }


def _verify_signature(body_bytes: bytes, signature: str, secret: str) -> bool:
    expected = hmac.HMAC(secret.encode(), body_bytes, digestmod=hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


def _find_or_create_contact(conn, org_id: str, phone: str) -> str:
    if phone:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT contact_id FROM firm_os.contacts WHERE org_id = %s AND phone = %s",
                (org_id, phone),
            )
            row = cur.fetchone()
        if row:
            return str(row['contact_id'])

    contact_id = str(uuid.uuid4())
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO firm_os.contacts (contact_id, org_id, phone, status, created_at) "
            "VALUES (%s, %s, %s, 'active', NOW())",
            (contact_id, org_id, phone or None),
        )
    conn.commit()
    return contact_id


def _contains_emergency(transcript: list) -> bool:
    for turn in transcript:
        text = (turn.get('message') or '').lower()
        if any(p.search(text) for p in EMERGENCY_PATTERNS):
            return True
    return False


def lambda_handler(event, context):
    body_str = event.get('body') or ''
    body_bytes = body_str.encode()

    # Verify ElevenLabs HMAC signature — required in all environments
    try:
        secret = _get_secret('firmos/voice/webhook-secret')['secret']
        headers = {k.lower(): v for k, v in (event.get('headers') or {}).items()}
        signature = headers.get('x-elevenlabs-signature', '')
        if not signature or not _verify_signature(body_bytes, signature, secret):
            logger.warning("Missing or invalid ElevenLabs webhook signature")
            return _resp(401, {'error': 'invalid_signature'})
    except KeyError:
        logger.error("Secrets Manager key missing — cannot verify signature, rejecting")
        return _resp(500, {'error': 'secret_configuration_error'})
    except Exception as exc:
        logger.error("Signature check failed: %s", exc)
        return _resp(500, {'error': 'signature_check_error'})

    try:
        payload = json.loads(body_str)
    except Exception:
        return _resp(400, {'error': 'invalid_json'})

    if payload.get('type') != 'post_call_transcription':
        return _resp(200, {'status': 'ignored', 'type': payload.get('type')})

    data = payload.get('data', {})
    agent_id = data.get('agent_id', '')
    conversation_id = data.get('conversation_id', '')
    transcript = data.get('transcript', [])
    metadata = data.get('metadata', {})
    analysis = data.get('analysis', {})
    custom_data = (data.get('conversation_initiation_client_data') or {}).get('custom_llm_extra_body', {})
    caller_phone = metadata.get('caller_id', '')

    conn = get_connection()

    # Resolve org from DB via agent_id — authoritative source
    with conn.cursor() as cur:
        cur.execute(
            "SELECT org_id FROM firm_os.organizations "
            "WHERE elevenlabs_agent_id = %s AND status = 'active'",
            (agent_id,),
        )
        row = cur.fetchone()
    if not row:
        logger.error("No org found for agent_id %s", agent_id)
        return _resp(404, {'error': 'org_not_found'})
    org_id = str(row['org_id'])

    # Cross-validate caller-supplied org_id if present
    supplied_org_id = custom_data.get('org_id')
    if supplied_org_id and supplied_org_id != org_id:
        logger.warning("org_id mismatch: agent resolves to %s but caller sent %s", org_id, supplied_org_id)
        return _resp(403, {'error': 'org_id_mismatch'})

    contact_id = _find_or_create_contact(conn, org_id, caller_phone)

    # Determine if escalated
    escalated = _contains_emergency(transcript) or not analysis.get('call_successful', True)
    duration_secs = metadata.get('call_duration_secs', 0)

    # Create conversation row
    conv_id = str(uuid.uuid4())
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO firm_os.conversations "
            "(conversation_id, org_id, contact_id, channel, state, started_at, ended_at, escalated) "
            "VALUES (%s, %s, %s, 'voice', %s, NOW(), NOW(), %s)",
            (conv_id, org_id, contact_id, 'escalated' if escalated else 'complete', escalated),
        )
    conn.commit()

    # Insert transcript turns as messages (batch)
    if transcript:
        message_rows = [
            (org_id, conv_id,
             'outbound' if turn.get('role') == 'agent' else 'inbound',
             turn.get('message', ''))
            for turn in transcript
        ]
        with conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO firm_os.messages "
                "(org_id, conversation_id, direction, body, created_at) "
                "VALUES (%s, %s, %s, %s, NOW())",
                message_rows,
            )
        conn.commit()

    if escalated:
        try:
            boto3.client('lambda', region_name=REGION).invoke(
                FunctionName='firmos-escalation',
                InvocationType='Event',
                Payload=json.dumps({
                    'org_id': org_id,
                    'contact_id': contact_id,
                    'conversation_id': conv_id,
                    'triggered_keyword': 'voice_post_call',
                    'message_body': analysis.get('transcript_summary', 'Voice call flagged for review'),
                    'channel': 'voice',
                }).encode(),
            )
        except Exception as exc:
            logger.warning("escalation invoke failed: %s", exc)

    log_audit(conn, org_id, 'voice-webhook', 'voice.call_completed', {
        'contact_id': contact_id,
        'conversation_id': conv_id,
        'duration_secs': duration_secs,
        'escalated': escalated,
        'summary': analysis.get('transcript_summary', ''),
    })

    return _resp(200, {'status': 'ok', 'conversation_id': conv_id})
