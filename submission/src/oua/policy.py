"""Versioned business policy (config/agent_policy.json). Validated on load so a bad edit fails loudly at start-up."""

from __future__ import annotations

import json
from functools import cached_property
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from oua.settings import SUBMISSION_ROOT

DEFAULT_POLICY = SUBMISSION_ROOT / "config" / "agent_policy.json"

KNOWN_CHECKS = {"signed_up_within_days", "no_prior_event", "subscription_status_in", "never_paid", "recent_event",
                "unfinished_series", "max_event_days", "no_event_in_window", "account_min_age_days"}
KNOWN_MATCHERS = {"later_ist_day_than_start", "property_not", "same_content_as_plan",
                  "title_released_within_days_before_start", "distinct_ist_days"}
KNOWN_HEROES = {"deeplink", "retrieved", "popular", "short_episodes", "goal_series", "new_release"}


class Interpretation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    extra_checks: list[dict[str, Any]] = Field(default_factory=list)
    assumption: str = ""

    @field_validator("extra_checks")
    @classmethod
    def _known(cls, v: list[dict[str, Any]]) -> list[dict[str, Any]]:
        for c in v:
            if c.get("check") not in KNOWN_CHECKS:
                raise ValueError(f"unknown eligibility check {c.get('check')!r}; known: {sorted(KNOWN_CHECKS)}")
        return v


class Policy(BaseModel):
    model_config = ConfigDict(extra="allow")

    version: str
    trigger: dict[str, Any]
    history: dict[str, Any]
    goal_priority: list[str]
    goal_labels: dict[str, str]
    goal_interpretations: dict[str, Interpretation]
    text_only_eligibility_without_interpretation: str = "refuse"
    plans: dict[str, Any]
    success_matchers: dict[str, list[dict[str, Any]]]
    page: dict[str, Any]
    row_sources: dict[str, dict[str, Any]]
    interventions: dict[str, dict[str, Any]]
    copy_templates: dict[str, dict[str, str]]
    whatsapp_template_for_titles: str
    safety: dict[str, Any]
    retrieval: dict[str, Any]
    llm: dict[str, Any]
    confidence: dict[str, Any]

    @field_validator("success_matchers")
    @classmethod
    def _matchers(cls, v: dict[str, list[dict[str, Any]]]) -> dict[str, list[dict[str, Any]]]:
        for goal, ms in v.items():
            for m in ms:
                if m.get("match") not in KNOWN_MATCHERS:
                    raise ValueError(f"{goal}: unknown success matcher {m.get('match')!r}; known: {sorted(KNOWN_MATCHERS)}")
        return v

    @field_validator("interventions")
    @classmethod
    def _interventions(cls, v: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
        if "default" not in v:
            raise ValueError("interventions.default is required")
        for goal, iv in v.items():
            for h in iv.get("hero", []):
                if h not in KNOWN_HEROES:
                    raise ValueError(f"{goal}: unknown hero strategy {h!r}")
        return v

    @classmethod
    def load(cls, path: Path | None = None) -> "Policy":
        p = Path(path or DEFAULT_POLICY)
        policy = cls.model_validate(json.loads(p.read_text(encoding="utf-8")))
        for goal, iv in policy.interventions.items():
            for row in iv.get("rows", []):
                if row not in policy.row_sources:
                    raise ValueError(f"{goal}: row source {row!r} is not defined in row_sources")
        return policy

    # -- convenience --------------------------------------------------------------------------------------------

    @property
    def ref(self) -> str:
        return f"policy:agent_policy@{self.version}"

    def intervention(self, goal_id: str) -> dict[str, Any]:
        merged = dict(self.interventions["default"])
        merged.update(self.interventions.get(goal_id, {}))
        return merged

    def label(self, goal_id: str) -> str:
        return self.goal_labels.get(goal_id, goal_id)

    def keep_without_recheck(self, goal_id: str) -> bool:
        return goal_id in self.plans.get("keep_while_open_without_recheck", [])

    @cached_property
    def conf_terms(self) -> dict[str, float]:
        return self.confidence["terms"]
