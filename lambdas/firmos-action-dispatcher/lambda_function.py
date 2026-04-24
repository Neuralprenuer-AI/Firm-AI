"""
firmos-action-dispatcher  —  AWS Lambda handler

Executes all side effects for an AgentResponse produced by firmos-agent-core.

Steps (in order):
  1. State transition  — update conversations + append conversation_state_log
  2. Intake upsert     — persist IntakeFields to intake_records
  3. ABA disclaimer    — prepend firm disclaimer to first message for new contacts
  4. Escalation        — async-invoke firmos-escalation if triggered
  5. SMS dispatch      — invoke firmos-twilio-send per message
  6. Persist outbound  — insert outbound messages into firm_os.messages
  7. Turn counter      — bump conversations.turn_count + last_message_at
  8. Audit log         — log_audit

Event contract:
{
  "agent_response":  { ...AgentResponse JSON... },
  "org_id":          "uuid",
  "contact_id":      "uuid",
  "conversation_id": "uuid",
  "contact_phone":   "+1...",
  "is_new_contact":  false
}

Region: us-east-2  |  Layer: /opt/python
"""
from __future__ import annotations

import json
import logging
import sys
import uuid
from typing import Any, Dict, List, Optional

sys.path.insert(0, "/opt/python")

import boto3
from botocore.exceptions import ClientError

from firmos_models import AgentResponse
from shared_db import get_connection, log_audit

try:
    from psycopg2.extras import Json
except ImportError:
    Json = None

logger = logging.getLogger()
logger.setLevel(logging.INFO)

REGION = "us-east-2"
TWILIO_SEND_FUNCTION = "firmos-twilio-send"
ESCALATION_FUNCTION = "firmos-escalation"

_lambda_client = boto3.client("lambda", region_name=REGION)
_secrets_client = boto3.client("secretsmanager", region_name=REGION)

_org_secret_cache: Dict[str, Dict[str, Any]] = {}


def _load_org_secret(org_id: str) -> Dict[str, Any]:
    if org_id in _org_secret_cache:
        return _org_secret_cache[org_id]

    conn = get_connection()
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT secret_arn,
                   COALESCE(firm_profile->>'disclaimer_text', '') AS disclaimer_text,
                   COALESCE(firm_profile->>'firm_name', name) AS firm_name
              FROM firm_os.organizations
             WHERE org_id = %s
            """,
            (org_id,),
        )
        row = cur.fetchone()

    if not row:
        raise RuntimeError(f"Unknown org_id={org_id}")

    secret_arn = row.get("secret_arn")
    twilio_auth_token = None
    if secret_arn:
        try:
            s = _secrets_client.get_secret_value(SecretId=secret_arn)
            secret_data = json.loads(s["SecretString"])
            twilio_auth_token = secret_data.get("twilio_auth_token")
        except ClientError as e:
            logger.error("secret load failed for org=%s arn=%s: %s", org_id, secret_arn, e)

    bundle = {
        "twilio_auth_token": twilio_auth_token,
        "disclaimer_text": row.get("disclaimer_text") or "",
        "firm_name": row.get("firm_name"),
    }
    _org_secret_cache[org_id] = bundle
    return bundle


def _apply_disclaimer(messages: List[str], disclaimer_text: str, is_new_contact: bool) -> List[str]:
    if not is_new_contact or not disclaimer_text or not messages:
        return messages
    out = list(messages)
    prefix = disclaimer_text.strip()
    first = out[0]
    if prefix[:20].lower() in first.lower():
        return out
    combined = f"{prefix}\n\n{first}"
    if len(combined) > 320:
        combined = combined[:317] + "..."
    out[0] = combined
    return out


def _update_conversation_state(
    *,
    conversation_id: str,
    org_id: str,
    new_mode: str,
    next_action: str,
    reasoning: str,
) -> Optional[str]:
    state_map = {
        "continue": "active",
        "escalate": "escalated",
        "complete_intake": "intake_complete",
        "close": "closed",
        "handoff_human": "escalated",
    }
    new_state = state_map.get(next_action, "active")

    conn = get_connection()
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT state, mode
              FROM firm_os.conversations
             WHERE conversation_id = %s AND org_id = %s
             FOR UPDATE
            """,
            (conversation_id, org_id),
        )
        prev = cur.fetchone()
        if not prev:
            raise RuntimeError(f"conversation {conversation_id} not found in org {org_id}")
        prev_state = prev["state"]

        cur.execute(
            """
            UPDATE firm_os.conversations
               SET previous_state    = state,
                   state             = %s,
                   mode              = %s,
                   state_updated_at  = now()
             WHERE conversation_id = %s AND org_id = %s
            """,
            (new_state, new_mode, conversation_id, org_id),
        )

        cur.execute(
            """
            INSERT INTO firm_os.conversation_state_log
                (id, org_id, conversation_id, from_state, to_state,
                 mode, next_action, reasoning, created_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, now())
            """,
            (str(uuid.uuid4()), org_id, conversation_id, prev_state, new_state, new_mode, next_action, reasoning),
        )
    conn.commit()
    return prev_state


def _upsert_intake(
    *,
    org_id: str,
    contact_id: str,
    conversation_id: str,
    fields_collected: Dict[str, Any],
    completion_percent: int,
    finalize: bool,
) -> Optional[str]:
    status = "submitted" if finalize else "in_progress"
    fields_json = Json(fields_collected) if Json else json.dumps(fields_collected)

    conn = get_connection()
    with conn.cursor() as cur:
        cur.execute(
            "SELECT intake_id FROM firm_os.intake_records WHERE org_id = %s AND conversation_id = %s LIMIT 1",
            (org_id, conversation_id),
        )
        row = cur.fetchone()

        if row:
            intake_id = str(row["intake_id"])
            cur.execute(
                """
                UPDATE firm_os.intake_records
                   SET fields             = %s,
                       completion_percent = %s,
                       status             = %s,
                       full_name          = %s,
                       phone_verified     = %s,
                       preferred_language = %s,
                       case_type          = %s,
                       urgency            = %s,
                       detention_status   = %s,
                       brief_description  = %s,
                       updated_at         = now(),
                       closed_at          = CASE WHEN %s THEN now() ELSE closed_at END
                 WHERE intake_id = %s AND org_id = %s
                """,
                (
                    fields_json, completion_percent, status,
                    fields_collected.get("full_name"),
                    fields_collected.get("phone_verified"),
                    fields_collected.get("preferred_language"),
                    fields_collected.get("case_type"),
                    fields_collected.get("urgency"),
                    fields_collected.get("detention_status"),
                    fields_collected.get("brief_description"),
                    finalize, intake_id, org_id,
                ),
            )
        else:
            intake_id = str(uuid.uuid4())
            cur.execute(
                """
                INSERT INTO firm_os.intake_records
                    (intake_id, org_id, contact_id, conversation_id, status,
                     fields, completion_percent,
                     full_name, phone_verified, preferred_language,
                     case_type, urgency, detention_status, brief_description,
                     created_at, updated_at, closed_at)
                VALUES
                    (%s, %s, %s, %s, %s, %s, %s,
                     %s, %s, %s, %s, %s, %s, %s,
                     now(), now(), CASE WHEN %s THEN now() ELSE NULL END)
                """,
                (
                    intake_id, org_id, contact_id, conversation_id, status,
                    fields_json, completion_percent,
                    fields_collected.get("full_name"),
                    fields_collected.get("phone_verified"),
                    fields_collected.get("preferred_language"),
                    fields_collected.get("case_type"),
                    fields_collected.get("urgency"),
                    fields_collected.get("detention_status"),
                    fields_collected.get("brief_description"),
                    finalize,
                ),
            )
    conn.commit()
    return intake_id


def _invoke_escalation(
    *, org_id: str, conversation_id: str, contact_id: str,
    contact_phone: str, escalation: Dict[str, Any], intake_fields: Dict[str, Any],
) -> None:
    payload = {
        "org_id": org_id,
        "conversation_id": conversation_id,
        "contact_id": contact_id,
        "contact_phone": contact_phone,
        "severity": escalation.get("severity"),
        "reason": escalation.get("reason"),
        "attorney_summary": escalation.get("attorney_summary"),
        "intake_fields": intake_fields,
    }
    try:
        _lambda_client.invoke(
            FunctionName=ESCALATION_FUNCTION,
            InvocationType="Event",
            Payload=json.dumps(payload).encode("utf-8"),
        )
    except ClientError as e:
        logger.error("escalation invoke failed: %s", e)


def _invoke_crm_push(
    *, org_id: str, contact_id: str, intake_id: str, conversation_id: str
) -> None:
    if not intake_id:
        logger.warning("crm-push skipped: no intake_id available")
        return
    payload = {
        "org_id": org_id,
        "contact_id": contact_id,
        "intake_id": intake_id,
        "conversation_id": conversation_id,
    }
    try:
        _lambda_client.invoke(
            FunctionName="firmos-crm-push",
            InvocationType="Event",
            Payload=json.dumps(payload).encode("utf-8"),
        )
        logger.info("crm-push invoked for intake_id=%s", intake_id)
    except ClientError as e:
        logger.error("crm-push invoke failed: %s", e)


def _send_sms(*, org_id: str, to_phone: str, body: str, conversation_id: str, subaccount_token: Optional[str]) -> None:
    _lambda_client.invoke(
        FunctionName=TWILIO_SEND_FUNCTION,
        InvocationType="Event",
        Payload=json.dumps({
            "org_id": org_id,
            "to_phone": to_phone,
            "body": body,
            "conversation_id": conversation_id,
            "subaccount_token": subaccount_token,
        }).encode("utf-8"),
    )


def _bump_turn_count(conversation_id: str, org_id: str) -> None:
    conn = get_connection()
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE firm_os.conversations
               SET turn_count = COALESCE(turn_count, 0) + 1,
                   last_message_at = now()
             WHERE conversation_id = %s AND org_id = %s
            """,
            (conversation_id, org_id),
        )
    conn.commit()


def lambda_handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    request_id = getattr(context, "aws_request_id", str(uuid.uuid4()))
    logger.info("firmos-action-dispatcher start req=%s", request_id)

    try:
        agent_raw = event["agent_response"]
        agent = AgentResponse.model_validate(agent_raw)
        org_id = event["org_id"]
        contact_id = event["contact_id"]
        conversation_id = event["conversation_id"]
        contact_phone = event["contact_phone"]
        is_new_contact = bool(event.get("is_new_contact", False))
    except Exception as e:
        logger.exception("bad input: %s", e)
        return {"statusCode": 400, "error": f"bad_request: {e}"}

    state_update = agent.state_update
    intake_progress = agent.intake_progress
    fields_collected = intake_progress.fields_collected.model_dump()
    finalize_intake = state_update.next_action == "complete_intake"
    escalated = agent.escalation.triggered

    # 1. State transition
    try:
        prev_state = _update_conversation_state(
            conversation_id=conversation_id,
            org_id=org_id,
            new_mode=state_update.mode,
            next_action=state_update.next_action,
            reasoning=state_update.reasoning,
        )
    except Exception as e:
        logger.exception("state transition failed")
        return {"statusCode": 500, "error": f"state_transition: {e}"}

    # 2. Intake upsert
    intake_id: Optional[str] = None
    try:
        intake_id = _upsert_intake(
            org_id=org_id,
            contact_id=contact_id,
            conversation_id=conversation_id,
            fields_collected=fields_collected,
            completion_percent=intake_progress.completion_percent,
            finalize=finalize_intake,
        )
    except Exception as e:
        logger.exception("intake upsert failed (non-fatal): %s", e)

    # 3. Load org secret + apply disclaimer
    try:
        org_secret = _load_org_secret(org_id)
    except Exception as e:
        logger.exception("org secret load failed: %s", e)
        org_secret = {"twilio_auth_token": None, "disclaimer_text": "", "firm_name": None}

    messages_out = _apply_disclaimer(
        list(agent.client_messages),
        org_secret.get("disclaimer_text") or "",
        is_new_contact,
    )

    # 4. Escalation async-invoke
    if escalated:
        _invoke_escalation(
            org_id=org_id,
            conversation_id=conversation_id,
            contact_id=contact_id,
            contact_phone=contact_phone,
            escalation=agent.escalation.model_dump(),
            intake_fields=fields_collected,
        )

    # 4b. CRM push — async-invoke on intake completion
    if finalize_intake and intake_id:
        _invoke_crm_push(
            org_id=org_id,
            contact_id=contact_id,
            intake_id=intake_id,
            conversation_id=conversation_id,
        )

    # 5. Send SMS (twilio-send Lambda owns message persistence)
    send_errors: List[str] = []
    for body in messages_out:
        try:
            _send_sms(
                org_id=org_id,
                to_phone=contact_phone,
                body=body,
                conversation_id=conversation_id,
                subaccount_token=org_secret.get("twilio_auth_token"),
            )
        except Exception as e:
            send_errors.append(str(e))
            logger.exception("sms send failed")

    # 7. Turn count
    try:
        _bump_turn_count(conversation_id, org_id)
    except Exception as e:
        logger.warning("turn count bump failed: %s", e)

    # 8. Audit
    try:
        log_audit(
            get_connection(),
            org_id,
            "firmos-action-dispatcher",
            "agent_dispatch",
            {
                "request_id": request_id,
                "intent": agent.intent,
                "confidence": agent.confidence,
                "mode": state_update.mode,
                "next_action": state_update.next_action,
                "previous_state": prev_state,
                "escalated": escalated,
                "intake_id": intake_id,
                "messages_sent": len(messages_out),
                "send_errors": send_errors or None,
                "is_new_contact": is_new_contact,
            },
        )
    except Exception as e:
        logger.warning("audit log failed: %s", e)

    return {
        "statusCode": 200,
        "conversation_id": conversation_id,
        "state": state_update.next_action,
        "escalated": escalated,
        "intake_id": intake_id,
        "messages_sent": len(messages_out),
        "send_errors": send_errors,
    }
