"""Versioned decision and domain models. No viewer-specific constants."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from oua import SCHEMA_VERSION

PLACEHOLDER_TEXT = frozenset(
    {"n/a", "na", "none", "null", "placeholder", "tbd", "todo", "unknown", "-"}
)

# Signals that cannot be measured from the supplied event stream / simulator schema.
UNMEASURABLE_SUCCESS_EVENTS = frozenset({None, "", "tv_app_open"})


class ActionType(str, Enum):
    HOME_PAGE = "HOME_PAGE"
    PUSH = "PUSH"
    WHATSAPP = "WHATSAPP"
    IN_APP_MESSAGE = "IN_APP_MESSAGE"
    NO_ACTION = "NO_ACTION"


class DecisionStatus(str, Enum):
    PUBLISHED = "PUBLISHED"
    ABSTAINED = "ABSTAINED"
    FAILED_SAFE = "FAILED_SAFE"


class ExperimentArm(str, Enum):
    AGENT = "agent"
    HOLDOUT = "holdout"


PUBLISHABLE_ACTIONS = frozenset(
    {
        ActionType.HOME_PAGE,
        ActionType.PUSH,
        ActionType.WHATSAPP,
        ActionType.IN_APP_MESSAGE,
    }
)


class TriggerRef(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event: str
    event_id: str
    received_at: datetime
    deliver_at: datetime | None = None
    event_age_s: float | None = None


class SuccessSignal(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event: str
    condition: str
    window_hours: int = Field(gt=0)

    @field_validator("event")
    @classmethod
    def event_must_be_measurable(cls, value: str) -> str:
        if value in UNMEASURABLE_SUCCESS_EVENTS or not value.strip():
            raise ValueError(
                "success signal event is missing or unmeasurable; do not invent a proxy"
            )
        return value


class GoalRef(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    deadline: datetime
    success_signal: SuccessSignal
    rule_version: str


class DialectInfo(BaseModel):
    model_config = ConfigDict(extra="forbid")

    used: str | None = None
    source: str | None = None
    conflicts: list[str] = Field(default_factory=list)


class EvidenceDetail(BaseModel):
    """One cited source. Omit rather than fabricate timestamps or IDs."""

    model_config = ConfigDict(extra="forbid")

    id: str
    source: str
    timestamp: datetime | None = None
    freshness_s: float | None = None
    note: str | None = None


def _reject_placeholder(value: str, field_name: str) -> str:
    cleaned = value.strip()
    if not cleaned or cleaned.lower() in PLACEHOLDER_TEXT:
        raise ValueError(f"{field_name} must be a real statement, not a placeholder")
    return cleaned


class ConflictRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    fact: str
    sources: list[str] = Field(min_length=2)
    chosen: str | None
    why: str

    @field_validator("why")
    @classmethod
    def why_required(cls, value: str) -> str:
        return _reject_placeholder(value, "conflict.why")


class Checks(BaseModel):
    model_config = ConfigDict(extra="forbid")

    passed: list[str] = Field(default_factory=list)
    failed: list[str] = Field(default_factory=list)


class ExecutionAck(BaseModel):
    """Executor/simulator acknowledgement. Required true only for PUBLISHED."""

    model_config = ConfigDict(extra="forbid")

    confirmed: bool
    confirmed_at: datetime | None = None
    executor: str | None = None


class Decision(BaseModel):
    """Canonical machine-readable decision (schema_version 1.0.0)."""

    model_config = ConfigDict(extra="forbid")

    schema_version: str = SCHEMA_VERSION
    decision_id: str
    viewer_id: str
    trigger: TriggerRef
    goal: GoalRef | None
    action: ActionType
    dialect: DialectInfo
    content: list[str] = Field(default_factory=list)
    confidence: float = Field(ge=0.0, le=1.0)
    confidence_reason: str
    evidence: list[str] = Field(default_factory=list)
    evidence_details: list[EvidenceDetail] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    conflicting_evidence: list[ConflictRecord] = Field(default_factory=list)
    checks: Checks
    decided_in_ms: int = Field(ge=0)
    decision_deadline_ms: int = Field(default=60_000, gt=0)
    status: DecisionStatus
    abstention_reason: str | None = None
    failure_reason: str | None = None
    execution: ExecutionAck | None = None

    @field_validator("confidence_reason")
    @classmethod
    def reason_required(cls, value: str) -> str:
        return _reject_placeholder(value, "confidence_reason")

    @field_validator("assumptions")
    @classmethod
    def no_blank_or_placeholder_assumptions(cls, value: list[str]) -> list[str]:
        return [_reject_placeholder(item, "assumptions") for item in value if item.strip()]

    @model_validator(mode="after")
    def enforce_combinations_and_evidence(self) -> "Decision":
        detail_ids = [item.id for item in self.evidence_details]
        if len(detail_ids) != len(set(detail_ids)):
            raise ValueError("evidence_details ids must be unique")
        if set(self.evidence) != set(detail_ids):
            raise ValueError(
                "every evidence id must have a matching evidence_details row and vice versa; "
                "do not cite unresolved ids"
            )
        if self.goal is not None and not self.goal.id.strip():
            raise ValueError("goal.id must be a real rule id")

        confirmed = bool(self.execution and self.execution.confirmed)

        if self.status == DecisionStatus.PUBLISHED:
            if self.action not in PUBLISHABLE_ACTIONS:
                raise ValueError("PUBLISHED requires a publishable action, not NO_ACTION")
            if self.goal is None:
                raise ValueError("PUBLISHED requires a selected measurable goal")
            if not confirmed:
                raise ValueError("PUBLISHED requires confirmed executor/simulator success")
            if self.abstention_reason:
                raise ValueError("PUBLISHED cannot include abstention_reason")
            if self.failure_reason:
                raise ValueError("PUBLISHED cannot include failure_reason")
            if self.checks.failed:
                raise ValueError("PUBLISHED requires independent checks.failed to be empty")
        elif self.status == DecisionStatus.ABSTAINED:
            if self.action != ActionType.NO_ACTION:
                raise ValueError("ABSTAINED requires action NO_ACTION")
            if self.abstention_reason is None:
                raise ValueError("ABSTAINED requires a genuine abstention_reason")
            self.abstention_reason = _reject_placeholder(self.abstention_reason, "abstention_reason")
            if confirmed:
                raise ValueError("ABSTAINED cannot have execution.confirmed true")
            if self.content:
                raise ValueError("ABSTAINED cannot publish content ids")
        elif self.status == DecisionStatus.FAILED_SAFE:
            if self.failure_reason is None:
                raise ValueError("FAILED_SAFE requires failure_reason")
            self.failure_reason = _reject_placeholder(self.failure_reason, "failure_reason")
            if confirmed:
                raise ValueError("FAILED_SAFE cannot have execution.confirmed true")
            if self.action != ActionType.NO_ACTION:
                raise ValueError("FAILED_SAFE records that publication did not occur")
            if self.content:
                raise ValueError("FAILED_SAFE cannot publish content ids")
        return self

    def to_runtime_dict(self) -> dict[str, Any]:
        """Single JSON object for CLI/replay (Pydantic dump, JSON-ready)."""
        return self.model_dump(mode="json")
