"""
firmos-agent-core  —  AWS Lambda handler

Stateless pure function. Loads context from RDS, builds system prompt, calls
Gemini with structured JSON mode, then async-invokes firmos-action-dispatcher.

Event contract:
{
  "org_id":          "uuid",
  "contact_id":      "uuid",
  "conversation_id": "uuid",
  "user_message":    "...",
  "contact_phone":   "+1...",
  "is_new_contact":  false,
  "current_mode":    "emergency|intake|faq|returning|closed"   # optional
}

Region: us-east-2  |  Secret: rcm/gemini/api-key  |  Layer: /opt/python
"""
from __future__ import annotations

import json
import logging
import sys
import time
import uuid
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, "/opt/python")

import boto3
from botocore.exceptions import ClientError

from firmos_models import (
    AgentResponse,
    ContactHistorySummary,
    FirmProfile,
    IntakeFields,
    IntakeProgress,
)
from firmos_prompts import build_system_prompt
from shared_db import get_connection

try:
    import google.genai as genai
    from google.genai import types as genai_types
    _HAVE_GENAI_SDK = True
except Exception:
    genai = None
    genai_types = None
    _HAVE_GENAI_SDK = False

import re
import requests

logger = logging.getLogger()
logger.setLevel(logging.INFO)

GEMINI_SECRET_ID = "rcm/gemini/api-key"
REGION = "us-east-2"
GEMINI_MODEL_PRIMARY = "gemini-2.0-flash"
GEMINI_MODEL_FALLBACK = "gemini-2.5-flash"
GEMINI_TIMEOUT_SECS = 20
GEMINI_TEMPERATURE = 0.4
GEMINI_MAX_OUTPUT_TOKENS = 8192
RECENT_MESSAGES_LIMIT = 15

_secrets_client = boto3.client("secretsmanager", region_name=REGION)
_lambda_client = boto3.client("lambda", region_name=REGION)
_cached_api_key: Optional[str] = None


def _get_gemini_api_key() -> str:
    global _cached_api_key
    if _cached_api_key:
        return _cached_api_key
    resp = _secrets_client.get_secret_value(SecretId=GEMINI_SECRET_ID)
    secret = json.loads(resp["SecretString"])
    key = secret.get("api_key")
    if not key:
        raise RuntimeError(f"Secret {GEMINI_SECRET_ID} missing 'api_key'")
    _cached_api_key = key
    return key


def _load_firm_profile(org_id: str) -> Tuple[str, Dict[str, Any]]:
    conn = get_connection()
    with conn.cursor() as cur:
        cur.execute(
            "SELECT name, firm_profile FROM firm_os.organizations WHERE org_id = %s",
            (org_id,),
        )
        row = cur.fetchone()

    if not row:
        raise RuntimeError(f"Unknown org_id={org_id}")

    firm_name = row["name"]
    profile_raw = row.get("firm_profile") or {}
    try:
        profile = FirmProfile.model_validate(
            {**profile_raw, "firm_name": profile_raw.get("firm_name") or firm_name}
        ).model_dump(mode="json")
    except Exception as e:
        logger.warning("firm_profile invalid for org %s: %s — using minimal", org_id, e)
        profile = FirmProfile(firm_name=firm_name).model_dump(mode="json")

    return firm_name, profile


def _load_contact_history(
    org_id: str, contact_id: str
) -> Tuple[bool, Optional[Dict[str, Any]]]:
    conn = get_connection()
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT contact_id, profile_summary, total_conversations,
                   last_intake_at, last_contact_at
              FROM firm_os.contacts
             WHERE org_id = %s AND contact_id = %s
            """,
            (org_id, contact_id),
        )
        contact = cur.fetchone()

        if not contact:
            return True, None

        cur.execute(
            """
            SELECT intake_id AS matter_id, case_type, status,
                   created_at AS opened_at, closed_at, brief_description AS summary
              FROM firm_os.intake_records
             WHERE org_id = %s AND contact_id = %s
             ORDER BY created_at DESC
             LIMIT 5
            """,
            (org_id, contact_id),
        )
        prior = cur.fetchall() or []

    summary_raw = contact.get("profile_summary") or {}
    prior_norm = [
        {
            "matter_id": str(p["matter_id"]) if p.get("matter_id") else None,
            "case_type": p.get("case_type"),
            "status": p.get("status"),
            "opened_at": p["opened_at"].isoformat() if p.get("opened_at") else None,
            "closed_at": p["closed_at"].isoformat() if p.get("closed_at") else None,
            "summary": p.get("summary"),
        }
        for p in prior
    ]

    merged = {
        **summary_raw,
        "total_conversations": contact.get("total_conversations") or 0,
        "last_contact_at": (
            contact["last_contact_at"].isoformat()
            if contact.get("last_contact_at") else None
        ),
        "last_intake_at": (
            contact["last_intake_at"].isoformat()
            if contact.get("last_intake_at") else None
        ),
        "prior_matters": prior_norm,
    }
    try:
        history = ContactHistorySummary.model_validate(merged).model_dump(mode="json")
    except Exception as e:
        logger.warning("contact profile_summary invalid for %s: %s", contact_id, e)
        history = merged

    is_new = (contact.get("total_conversations") or 0) == 0
    return is_new, history


def _load_recent_messages(conversation_id: str, limit: int) -> List[str]:
    conn = get_connection()
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT direction, body
              FROM firm_os.messages
             WHERE conversation_id = %s
             ORDER BY created_at DESC
             LIMIT %s
            """,
            (conversation_id, limit),
        )
        rows = cur.fetchall() or []

    rows = list(reversed(rows))
    lines = []
    for r in rows:
        role = "client" if r.get("direction") == "inbound" else "maria"
        body = (r.get("body") or "").replace("\n", " ").strip()
        if body:
            lines.append(f"{role}: {body}")
    return lines


def _strip_json_fences(text: str) -> str:
    text = text.strip()
    text = re.sub(r'^```(?:json)?\s*', '', text)
    text = re.sub(r'\s*```$', '', text)
    return text.strip()


def _build_gemini_schema() -> Dict[str, Any]:
    """Resolve all $ref inline so Gemini's responseSchema has no external refs."""
    schema = AgentResponse.model_json_schema()
    defs = schema.pop("$defs", {})

    def resolve(node: Any) -> Any:
        if isinstance(node, dict):
            if "$ref" in node:
                ref_name = node["$ref"].split("/")[-1]
                return resolve(dict(defs.get(ref_name, {})))
            drop = {"title", "examples", "additionalProperties"}
            return {k: resolve(v) for k, v in node.items() if k not in drop}
        if isinstance(node, list):
            return [resolve(x) for x in node]
        return node

    return resolve(schema)


_GEMINI_SCHEMA: Optional[Dict[str, Any]] = None


def _get_gemini_schema() -> Dict[str, Any]:
    global _GEMINI_SCHEMA
    if _GEMINI_SCHEMA is None:
        _GEMINI_SCHEMA = _build_gemini_schema()
    return _GEMINI_SCHEMA


def _call_gemini(*, system_prompt: str, user_message: str, model_name: str, api_key: str) -> str:
    if _HAVE_GENAI_SDK:
        client = genai.Client(api_key=api_key)
        resp = client.models.generate_content(
            model=model_name,
            contents=user_message,
            config=genai_types.GenerateContentConfig(
                system_instruction=system_prompt,
                response_mime_type="application/json",
                temperature=GEMINI_TEMPERATURE,
                max_output_tokens=GEMINI_MAX_OUTPUT_TOKENS,
            ),
        )
        text = getattr(resp, "text", None)
        if not text:
            try:
                text = resp.candidates[0].content.parts[0].text
            except Exception as e:
                raise RuntimeError(f"Gemini SDK empty response: {e}")
        return text

    url = (
        f"https://generativelanguage.googleapis.com/v1beta/models/"
        f"{model_name}:generateContent?key={api_key}"
    )
    body = {
        "system_instruction": {"parts": [{"text": system_prompt}]},
        "contents": [{"role": "user", "parts": [{"text": user_message}]}],
        "generationConfig": {
            "responseMimeType": "application/json",
            "responseSchema": _get_gemini_schema(),
            "temperature": GEMINI_TEMPERATURE,
            "maxOutputTokens": GEMINI_MAX_OUTPUT_TOKENS,
            "thinkingConfig": {"thinkingBudget": 1024},
        },
    }
    r = requests.post(url, json=body, timeout=GEMINI_TIMEOUT_SECS)
    if r.status_code in (429, 503):
        raise TimeoutError(f"Gemini {r.status_code} — retrying next model")
    if r.status_code >= 400:
        raise RuntimeError(f"Gemini HTTP {r.status_code}: {r.text[:400]}")
    data = r.json()
    try:
        parts = data["candidates"][0]["content"]["parts"]
        # Gemini 2.5 Flash returns thinking tokens as parts with thought=True;
        # the actual JSON response is the last non-thought part.
        text = None
        for part in reversed(parts):
            if not part.get("thought", False) and "text" in part:
                text = part["text"]
                break
        if not text:
            text = parts[0]["text"]
        finish_reason = data["candidates"][0].get("finishReason", "")
        if finish_reason == "MAX_TOKENS":
            logger.warning("Gemini hit MAX_TOKENS — response may be truncated")
        return text
    except (KeyError, IndexError, TypeError) as e:
        raise RuntimeError(f"Unexpected Gemini REST shape: {e} / {data}")


def _safe_handoff_response(*, detected_language: str = "en", reason: str = "agent_core_failure") -> Dict[str, Any]:
    if detected_language == "es":
        msg = (
            "Disculpa, estoy teniendo problemas tecnicos. Un miembro del equipo "
            "te contactara en breve para ayudarte."
        )
    else:
        msg = (
            "Sorry, I'm having a technical issue. A team member will follow up "
            "with you shortly to help."
        )

    resp = AgentResponse(
        intent="off_topic",
        confidence=0.0,
        detected_language=detected_language,
        client_messages=[msg],
        state_update={"mode": "faq", "next_action": "continue", "reasoning": f"Agent-core fallback: {reason}"},
        intake_progress={"fields_collected": IntakeFields().model_dump(), "fields_remaining": [], "completion_percent": 0},
        escalation={"triggered": False, "severity": "none", "reason": None, "attorney_summary": None},
    )
    return resp.model_dump(mode="json")


def lambda_handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    request_id = getattr(context, "aws_request_id", str(uuid.uuid4()))
    logger.info("firmos-agent-core start req=%s", request_id)

    try:
        org_id = event["org_id"]
        contact_id = event["contact_id"]
        conversation_id = event["conversation_id"]
        contact_phone = event["contact_phone"]
        user_message = (event.get("user_message") or "").strip()
        if not user_message:
            raise ValueError("user_message is empty")
    except (KeyError, ValueError) as e:
        logger.error("bad input: %s", e)
        return {"statusCode": 400, "error": f"bad_request: {e}"}

    is_new_contact = bool(event.get("is_new_contact", False))
    current_mode = event.get("current_mode") or "faq"

    try:
        firm_name, firm_profile = _load_firm_profile(org_id)
        computed_is_new, contact_history = _load_contact_history(org_id, contact_id)
        conversation_lines = _load_recent_messages(conversation_id, RECENT_MESSAGES_LIMIT)
    except Exception as e:
        logger.exception("context load failed: %s", e)
        agent_response = _safe_handoff_response(reason=f"context_load:{e}")
        _invoke_dispatcher(org_id, contact_id, conversation_id, contact_phone, is_new_contact, agent_response)
        return {"statusCode": 200, "agent_response": agent_response}

    if contact_history and (contact_history.get("total_conversations") or 0) > 0 and current_mode == "faq":
        current_mode = "returning"

    system_prompt = build_system_prompt(
        firm_name=firm_name,
        mode=current_mode,
        firm_profile=firm_profile,
        contact_history=contact_history,
        intake_progress=None,
        conversation_history_lines=conversation_lines,
        emergency_callback_minutes=int(firm_profile.get("emergency_callback_minutes") or 30),
    )

    try:
        api_key = _get_gemini_api_key()
    except ClientError as e:
        logger.exception("secrets manager error: %s", e)
        agent_response = _safe_handoff_response(reason="secret_fetch")
        _invoke_dispatcher(org_id, contact_id, conversation_id, contact_phone, is_new_contact, agent_response)
        return {"statusCode": 200, "agent_response": agent_response}

    last_err: Optional[str] = None
    agent_response: Optional[Dict[str, Any]] = None

    for model_name in [GEMINI_MODEL_PRIMARY, GEMINI_MODEL_FALLBACK]:
        try:
            t0 = time.time()
            raw = _call_gemini(
                system_prompt=system_prompt,
                user_message=user_message,
                model_name=model_name,
                api_key=api_key,
            )
            latency_ms = int((time.time() - t0) * 1000)
            logger.info("gemini ok req=%s model=%s latency_ms=%d", request_id, model_name, latency_ms)
            parsed = AgentResponse.model_validate(json.loads(_strip_json_fences(raw)))
            agent_response = parsed.model_dump(mode="json")
            break
        except TimeoutError as e:
            last_err = f"timeout_{model_name}:{e}"
            logger.warning("gemini timeout on %s: %s", model_name, e)
        except Exception as e:
            last_err = f"{type(e).__name__}_{model_name}:{e}"
            try:
                logger.error("gemini raw (first 500): %s", raw[:500] if 'raw' in dir() else "no raw")
            except Exception:
                pass
            logger.exception("gemini failure on %s", model_name)

    if agent_response is None:
        logger.error("all models failed req=%s last_err=%s", request_id, last_err)
        agent_response = _safe_handoff_response(reason=last_err or "unknown")

    _invoke_dispatcher(org_id, contact_id, conversation_id, contact_phone, is_new_contact, agent_response)
    return {"statusCode": 200, "agent_response": agent_response}


def _invoke_dispatcher(
    org_id: str,
    contact_id: str,
    conversation_id: str,
    contact_phone: str,
    is_new_contact: bool,
    agent_response: Dict[str, Any],
) -> None:
    payload = {
        "agent_response": agent_response,
        "org_id": org_id,
        "contact_id": contact_id,
        "conversation_id": conversation_id,
        "contact_phone": contact_phone,
        "is_new_contact": is_new_contact,
    }
    try:
        _lambda_client.invoke(
            FunctionName="firmos-action-dispatcher",
            InvocationType="Event",
            Payload=json.dumps(payload).encode("utf-8"),
        )
    except ClientError as e:
        logger.error("dispatcher invoke failed: %s", e)
