"""Machine-readable decision record (brief section 4, extended). Invariants are enforced on validation."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

ActionName = Literal["HOME_PAGE", "PUSH", "WHATSAPP", "IN_APP_MESSAGE", "NO_ACTION"]
Status = Literal["PUBLISHED", "NO_ACTION", "FAILED_SAFE", "FAILED_LATE"]


class Trigger(BaseModel):
    model_config = ConfigDict(extra="allow")
    event: str
    event_id: str
    received_at: str
    deliver_at: str
    event_age_s: float | None
    buffered_s: float | None
    live_visit: bool


class Goal(BaseModel):
    model_config = ConfigDict(extra="allow")
    id: str
    name: str
    deadline: str
    success_signal: dict[str, Any]
    rule_version: str
    started_at: str
    plan_id: int | None = None
    kept_from_previous_decision: bool = False


class Dialect(BaseModel):
    used: str | None
    source: str
    conflicts: list[str] = Field(default_factory=list)


class EvidenceDetail(BaseModel):
    model_config = ConfigDict(extra="allow")
    id: str
    source: str
    observed_at: str | None = None
    freshness_s: float | None = None


class Checks(BaseModel):
    passed: list[str] = Field(default_factory=list)
    failed: list[str] = Field(default_factory=list)


class Decision(BaseModel):
    model_config = ConfigDict(extra="allow")

    schema_version: str
    decision_id: str
    viewer_id: str
    arm: str | None
    trigger: Trigger
    goal: Goal | None
    action: ActionName
    actions: list[dict[str, Any]] = Field(default_factory=list)
    no_action_reason: str | None = None
    dialect: Dialect
    content: list[str] = Field(default_factory=list)
    confidence: float = Field(ge=0.0, le=1.0)
    confidence_reason: str = Field(min_length=10)
    evidence: list[str] = Field(default_factory=list)
    evidence_details: list[EvidenceDetail] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    conflicting_evidence: list[dict[str, Any]] = Field(default_factory=list)
    checks: Checks
    model: dict[str, Any]
    budget: dict[str, Any]
    decided_in_ms: int = Field(ge=0)
    queue_wait_ms: int = Field(ge=0)
    latency_ms: int = Field(ge=0)
    published_at: str | None
    status: Status

    @model_validator(mode="after")
    def _invariants(self) -> "Decision":
        ids = [d.id for d in self.evidence_details]
        if set(ids) != set(self.evidence) or len(ids) != len(set(ids)):
            raise ValueError("every cited evidence id needs exactly one evidence_details entry (and vice versa)")
        if self.status == "PUBLISHED":
            if self.action == "NO_ACTION" or not self.actions:
                raise ValueError("PUBLISHED requires at least one action")
            if self.goal is None:
                raise ValueError("PUBLISHED requires a committed goal")
            if self.no_action_reason:
                raise ValueError("PUBLISHED cannot carry a no_action_reason")
            if self.arm != "agent":
                raise ValueError("only the agent arm may receive actions")
        else:
            if self.action != "NO_ACTION" or self.actions or self.content:
                raise ValueError(f"{self.status} must not publish anything")
            if not (self.no_action_reason and self.no_action_reason.strip()):
                raise ValueError(f"{self.status} requires an explicit no_action_reason")
        return self
