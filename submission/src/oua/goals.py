"""Supported goal rules loaded from versioned config. Does not invent success signals."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from oua.schemas import UNMEASURABLE_SUCCESS_EVENTS, GoalRef, SuccessSignal


class GoalRule(BaseModel):
    model_config = ConfigDict(extra="ignore")

    goal_id: str
    name: str
    description: str = ""
    eligibility: dict[str, Any] = Field(default_factory=dict)
    success: dict[str, Any] = Field(default_factory=dict)

    @property
    def success_event(self) -> str | None:
        event = self.success.get("event")
        if event is None:
            return None
        text = str(event).strip()
        return text or None

    @property
    def is_measurable(self) -> bool:
        return self.success_event not in UNMEASURABLE_SUCCESS_EVENTS


class GoalCatalog:
    def __init__(self, version: str, notes: str, rules: dict[str, GoalRule]) -> None:
        self.version = version
        self.notes = notes
        self.rules = rules
        self._success_overrides: dict[str, dict[str, Any]] = {}
        self._version_tags: dict[str, str] = {}

    @classmethod
    def load(cls, path: Path) -> "GoalCatalog":
        payload = json.loads(path.read_text(encoding="utf-8"))
        rules = {item["goal_id"]: GoalRule.model_validate(item) for item in payload["goals"]}
        return cls(version=payload["version"], notes=payload.get("notes", ""), rules=rules)

    def get(self, goal_id: str) -> GoalRule:
        try:
            return self.rules[goal_id]
        except KeyError as exc:
            raise KeyError(f"unknown goal id {goal_id!r}; not in configured catalog") from exc

    def apply_success_correction(
        self,
        goal_id: str,
        new_success: dict[str, Any],
        *,
        version_tag: str,
    ) -> None:
        """Change success for one goal id. Does not rewrite unrelated rules."""
        self.get(goal_id)
        event = new_success.get("event")
        if event in (None, ""):
            raise ValueError("correction must name a real success event; refusing empty/null")
        self._success_overrides[goal_id] = dict(new_success)
        self._version_tags[goal_id] = version_tag

    def effective_success(self, goal_id: str) -> dict[str, Any]:
        rule = self.get(goal_id)
        if goal_id in self._success_overrides:
            return dict(self._success_overrides[goal_id])
        return dict(rule.success)

    def rule_version_for(self, goal_id: str) -> str:
        return self._version_tags.get(goal_id, self.version)

    def is_measurable(self, goal_id: str) -> bool:
        event = self.effective_success(goal_id).get("event")
        if event is None or str(event).strip() == "":
            return False
        return str(event).strip() not in UNMEASURABLE_SUCCESS_EVENTS

    def require_measurable(self, goal: GoalRef) -> GoalRule:
        rule = self.get(goal.id)
        if not self.is_measurable(goal.id):
            raise ValueError(
                f"goal {goal.id} has no supported measurable success signal in catalog version {self.rule_version_for(goal.id)}"
            )
        expected = str(self.effective_success(goal.id).get("event"))
        if goal.success_signal.event != expected:
            raise ValueError(
                f"goal {goal.id} success event {goal.success_signal.event!r} "
                f"does not match catalog {expected!r}; do not invent a proxy"
            )
        expected_version = self.rule_version_for(goal.id)
        if goal.rule_version != expected_version:
            raise ValueError(
                f"goal {goal.id} rule_version {goal.rule_version!r} does not match catalog {expected_version!r}"
            )
        catalog_hours = self.effective_success(goal.id).get("window_hours")
        if catalog_hours is not None and goal.success_signal.window_hours != int(catalog_hours):
            raise ValueError(
                f"goal {goal.id} window_hours {goal.success_signal.window_hours} "
                f"does not match catalog {catalog_hours}"
            )
        return rule

    def success_signal_for(self, goal_id: str) -> SuccessSignal:
        if not self.is_measurable(goal_id):
            raise ValueError(f"goal {goal_id} is not measurable; no success signal is available")
        success = self.effective_success(goal_id)
        return SuccessSignal(
            event=str(success["event"]),
            condition=str(success.get("condition") or ""),
            window_hours=int(success["window_hours"]),
        )
