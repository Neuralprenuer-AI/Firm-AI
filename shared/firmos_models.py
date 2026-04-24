"""
firmos_models.py
================
Pydantic v2 models for Firm-AI SMS chatbot structured output schema.

These models define:
  1. The strict JSON contract Gemini must return (AgentResponse and its children).
  2. The JSONB schemas persisted in organizations.firm_profile and
     contacts.profile_summary.

Usage:
    from firmos_models import AgentResponse, FirmProfile, ContactHistorySummary

    # Validate Gemini output
    parsed = AgentResponse.model_validate_json(gemini_raw_text)

    # Serialize for Lambda return
    payload = parsed.model_dump(mode="json")

Layer: /opt/python  (deployed via shared Lambda layer)
Python: 3.11+  |  pydantic: >=2.5
"""
from __future__ import annotations

from typing import List, Literal, Optional, Dict, Any
from pydantic import BaseModel, Field, ConfigDict, field_validator


# ---------------------------------------------------------------------------
# AgentResponse subtree — the structured JSON Gemini MUST return
# ---------------------------------------------------------------------------

class EscalationInfo(BaseModel):
    """Attorney-escalation signal emitted by the agent.

    When `triggered=True`, the action-dispatcher will async-invoke
    `firmos-escalation` and transition the conversation to `escalated`.
    `attorney_summary` is the 2-sentence briefing shown to the attorney in
    the escalation notification (SMS or email).
    """
    model_config = ConfigDict(extra="forbid")

    triggered: bool = Field(
        ..., description="Whether to escalate to a human attorney."
    )
    severity: Literal["none", "low", "medium", "high", "critical"] = Field(
        ..., description="Severity ranking — drives SLA for attorney callback."
    )
    reason: Optional[str] = Field(
        None, description="Short internal reason for escalation (not sent to client)."
    )
    attorney_summary: Optional[str] = Field(
        None,
        max_length=600,
        description="2-sentence briefing for the attorney receiving the escalation.",
    )


class IntakeFields(BaseModel):
    """Structured intake data collected so far in the conversation.

    Every field is Optional — the agent fills them in across turns.
    `detention_status` drives urgency/escalation logic in the dispatcher.
    """
    model_config = ConfigDict(extra="forbid")

    full_name: Optional[str] = None
    phone_verified: Optional[str] = Field(
        None, description="E.164 phone confirmed by the client (may differ from SMS from-number)."
    )
    preferred_language: Optional[Literal["en", "es", "mixed"]] = None
    case_type: Optional[str] = Field(
        None,
        description="asylum | family petition | DACA | removal defense | citizenship | visa | TPS | other",
    )
    urgency: Optional[str] = Field(
        None, description="Deadline, court date, or other time-critical fact."
    )
    detention_status: Optional[Literal["free", "detained", "family_detained"]] = None
    brief_description: Optional[str] = Field(
        None, max_length=1000, description="Client's own words describing the matter."
    )


class IntakeProgress(BaseModel):
    """Progress tracker for the 7-field intake flow."""
    model_config = ConfigDict(extra="forbid")

    fields_collected: IntakeFields
    fields_remaining: List[str] = Field(
        default_factory=list,
        description="Names of IntakeFields fields still to collect, in ask-order.",
    )
    completion_percent: int = Field(
        ..., ge=0, le=100, description="0..100 rough completion signal."
    )


class StateUpdate(BaseModel):
    """Conversation-state machine transition the agent is requesting."""
    model_config = ConfigDict(extra="forbid")

    mode: Literal["emergency", "intake", "faq", "returning", "closed"]
    next_action: Literal[
        "continue",
        "escalate",
        "complete_intake",
        "close",
        "handoff_human",
    ]
    reasoning: str = Field(
        ...,
        max_length=500,
        description="Internal reasoning — logged for audit, NEVER sent to the client.",
    )


class AgentResponse(BaseModel):
    """Top-level contract between firmos-agent-core and firmos-action-dispatcher."""
    model_config = ConfigDict(extra="ignore")

    intent: Literal[
        "emergency",
        "intake_new",
        "intake_continue",
        "faq",
        "status_check",
        "returning_client",
        "off_topic",
        "spam",
    ]
    confidence: float = Field(..., ge=0.0, le=1.0)
    detected_language: Literal["en", "es", "mixed"]
    client_messages: List[str] = Field(
        ...,
        min_length=1,
        max_length=2,
        description="1-2 SMS-ready strings, each <=320 chars. Sent verbatim to client.",
    )
    state_update: StateUpdate
    intake_progress: IntakeProgress
    escalation: EscalationInfo
    faq_answered: Optional[Dict[str, Any]] = Field(
        None,
        description="Optional structured FAQ metadata: {question_id, category, confidence}.",
    )
    flags: Dict[str, Any] = Field(
        default_factory=dict,
        description="Free-form flags the dispatcher may act on (e.g. {'spanish_attorney_needed': true}).",
    )

    @field_validator("client_messages")
    @classmethod
    def _enforce_sms_length(cls, v: List[str]) -> List[str]:
        """Hard-cap each outbound SMS at 320 chars (2 SMS segments)."""
        for i, msg in enumerate(v):
            if not msg or not msg.strip():
                raise ValueError(f"client_messages[{i}] is empty")
            if len(msg) > 320:
                raise ValueError(
                    f"client_messages[{i}] exceeds 320 chars (got {len(msg)})"
                )
        return v


# ---------------------------------------------------------------------------
# FirmProfile — JSONB stored in organizations.firm_profile
# ---------------------------------------------------------------------------

class FirmHours(BaseModel):
    """Firm operating hours per weekday (24h local, e.g. '08:00'-'17:00')."""
    model_config = ConfigDict(extra="forbid")

    monday: Optional[str] = None
    tuesday: Optional[str] = None
    wednesday: Optional[str] = None
    thursday: Optional[str] = None
    friday: Optional[str] = None
    saturday: Optional[str] = None
    sunday: Optional[str] = None
    timezone: str = Field(default="America/Chicago")


class FirmFAQ(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question_id: str
    question: str
    answer: str
    category: Optional[str] = None


class FirmAttorney(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    phone: Optional[str] = None
    email: Optional[str] = None
    languages: List[str] = Field(default_factory=list)
    practice_areas: List[str] = Field(default_factory=list)
    is_primary: bool = False


class FirmProfile(BaseModel):
    """Mirrors JSONB stored in organizations.firm_profile.

    Injected into the system prompt as FIRM_CONTEXT so the agent can answer
    FAQs authoritatively and personalize responses.
    """
    model_config = ConfigDict(extra="forbid")

    firm_name: str
    practice_areas: List[str] = Field(default_factory=list)
    languages_supported: List[Literal["en", "es", "mixed"]] = Field(
        default_factory=lambda: ["en", "es"]
    )
    consultation_fee: Optional[str] = Field(
        None, description="Human-readable fee, e.g. '$150 (credited if retained)' or 'Free'."
    )
    hours: FirmHours = Field(default_factory=FirmHours)
    phone: Optional[str] = None
    email: Optional[str] = None
    website: Optional[str] = None
    address: Optional[str] = None
    attorneys: List[FirmAttorney] = Field(default_factory=list)
    faqs: List[FirmFAQ] = Field(default_factory=list)
    emergency_callback_minutes: int = Field(
        default=30, description="SLA for attorney callback after emergency escalation."
    )
    disclaimer_text: Optional[str] = Field(
        None,
        description="ABA Rule 5.3 disclaimer prepended to first message of every new contact.",
    )


# ---------------------------------------------------------------------------
# ContactHistorySummary — JSONB stored in contacts.profile_summary
# ---------------------------------------------------------------------------

class PriorMatter(BaseModel):
    model_config = ConfigDict(extra="forbid")

    matter_id: Optional[str] = None
    case_type: Optional[str] = None
    status: Optional[str] = Field(None, description="open | closed | pending | referred")
    opened_at: Optional[str] = None
    closed_at: Optional[str] = None
    summary: Optional[str] = None


class ContactHistorySummary(BaseModel):
    """Compact contact memory injected into the prompt for returning clients.

    Keeps the prompt under control even for clients with long histories —
    the full message log is loaded separately (last N turns).
    """
    model_config = ConfigDict(extra="forbid")

    known_full_name: Optional[str] = None
    preferred_language: Optional[Literal["en", "es", "mixed"]] = None
    total_conversations: int = 0
    last_contact_at: Optional[str] = None
    last_intake_at: Optional[str] = None
    prior_matters: List[PriorMatter] = Field(default_factory=list)
    notes: Optional[str] = Field(
        None, max_length=1000, description="Attorney-authored notes for the agent."
    )
    do_not_ask: List[str] = Field(
        default_factory=list,
        description="Intake fields already known — do NOT re-prompt for these.",
    )


__all__ = [
    "EscalationInfo",
    "IntakeFields",
    "IntakeProgress",
    "StateUpdate",
    "AgentResponse",
    "FirmHours",
    "FirmFAQ",
    "FirmAttorney",
    "FirmProfile",
    "PriorMatter",
    "ContactHistorySummary",
]
