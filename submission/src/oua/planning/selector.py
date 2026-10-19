"""Deterministic measurable-goal selection. No LLM."""

from __future__ import annotations

from datetime import timedelta

from pydantic import BaseModel, ConfigDict, Field

from oua.goals import GoalCatalog
from oua.schemas import GoalRef, SuccessSignal
from oua.timeutil import as_utc, days_between, ist_date
from oua.viewer import ViewerSnapshot

# Documented in submission/docs/GOAL_SELECTION.md. Not per-viewer.
PRIORITY: tuple[str, ...] = (
    "G01",
    "G03",
    "G02",
    "G12",
    "G07",
    "G04",
    "G05",
    "G06",
    "G08",
    "G09",
)


class EligibilityResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    goal_id: str
    eligible: bool
    measurable: bool
    reasons: list[str]
    evidence_ids: list[str] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)


class SelectionResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    goal: GoalRef | None
    abstention_reason: str | None
    evaluations: list[EligibilityResult]
    kept_active: bool = False


def _ids(*values: str | None) -> list[str]:
    return [item for item in values if item]


class GoalSelector:
    def __init__(self, catalog: GoalCatalog) -> None:
        self.catalog = catalog

    def evaluate_all(self, snapshot: ViewerSnapshot) -> list[EligibilityResult]:
        results: list[EligibilityResult] = []
        for goal_id in self.catalog.rules:
            results.append(self.evaluate(goal_id, snapshot))
        return results

    def evaluate(self, goal_id: str, snapshot: ViewerSnapshot) -> EligibilityResult:
        if not self.catalog.is_measurable(goal_id):
            success = self.catalog.effective_success(goal_id)
            event = success.get("event")
            return EligibilityResult(
                goal_id=goal_id,
                eligible=False,
                measurable=False,
                reasons=[
                    f"success event {event!r} is missing or unobservable; refusing to invent a proxy"
                ],
            )
        handler = {
            "G01": self._g01,
            "G02": self._g02,
            "G03": self._g03,
            "G04": self._g04,
            "G05": self._g05,
            "G06": self._g06,
            "G07": self._g07,
            "G08": self._g08,
            "G09": self._g09,
            "G12": self._g12,
        }.get(goal_id)
        if handler is None:
            return EligibilityResult(
                goal_id=goal_id,
                eligible=False,
                measurable=True,
                reasons=[f"no evaluator for {goal_id}"],
            )
        return handler(snapshot)

    def select(
        self,
        snapshot: ViewerSnapshot,
        *,
        active_goal_id: str | None = None,
    ) -> SelectionResult:
        evaluations = self.evaluate_all(snapshot)
        eligible_ids = {item.goal_id for item in evaluations if item.eligible}
        if active_goal_id and active_goal_id in eligible_ids:
            return SelectionResult(
                goal=self._commit(active_goal_id, snapshot),
                abstention_reason=None,
                evaluations=evaluations,
                kept_active=True,
            )
        for goal_id in PRIORITY:
            if goal_id in eligible_ids:
                return SelectionResult(
                    goal=self._commit(goal_id, snapshot),
                    abstention_reason=None,
                    evaluations=evaluations,
                    kept_active=False,
                )
        reasons = "; ".join(
            f"{item.goal_id}: {item.reasons[0]}" for item in evaluations if item.reasons
        )
        return SelectionResult(
            goal=None,
            abstention_reason=reasons or "No measurable eligible goal.",
            evaluations=evaluations,
            kept_active=False,
        )

    def _commit(self, goal_id: str, snapshot: ViewerSnapshot) -> GoalRef:
        signal = self.catalog.success_signal_for(goal_id)
        start = as_utc(snapshot.decision_time)
        deadline = start + timedelta(hours=signal.window_hours)
        return GoalRef(
            id=goal_id,
            deadline=deadline,
            success_signal=SuccessSignal(
                event=signal.event,
                condition=signal.condition,
                window_hours=signal.window_hours,
            ),
            rule_version=self.catalog.rule_version_for(goal_id),
        )

    def _g01(self, snapshot: ViewerSnapshot) -> EligibilityResult:
        rule = self.catalog.get("G01").eligibility
        days = int(rule["signed_up_within_days"])
        if snapshot.signup_at is None:
            return EligibilityResult(
                goal_id="G01",
                eligible=False,
                measurable=True,
                reasons=["signup_at unknown; insufficient history for signed_up_within_days"],
            )
        age = days_between(snapshot.signup_at, snapshot.decision_time)
        if age > days:
            return EligibilityResult(
                goal_id="G01",
                eligible=False,
                measurable=True,
                reasons=[f"signup age {age:.2f}d exceeds {days}d"],
                evidence_ids=_ids(snapshot.signup_evidence_id),
            )
        if snapshot.play_started:
            return EligibilityResult(
                goal_id="G01",
                eligible=False,
                measurable=True,
                reasons=["requires_no_prior_event play_started is already present"],
                evidence_ids=_ids(snapshot.play_started[0].event_id, snapshot.signup_evidence_id),
            )
        return EligibilityResult(
            goal_id="G01",
            eligible=True,
            measurable=True,
            reasons=["signed up within window and no play_started"],
            evidence_ids=_ids(snapshot.signup_evidence_id),
        )

    def _g02(self, snapshot: ViewerSnapshot) -> EligibilityResult:
        if snapshot.episode_completed:
            return EligibilityResult(
                goal_id="G02",
                eligible=False,
                measurable=True,
                reasons=["requires_no_prior_event episode_completed is already present"],
                evidence_ids=_ids(snapshot.episode_completed[0].event_id),
            )
        return EligibilityResult(
            goal_id="G02",
            eligible=True,
            measurable=True,
            reasons=["no episode_completed on record"],
            evidence_ids=[],
        )

    def _g03(self, snapshot: ViewerSnapshot) -> EligibilityResult:
        days = int(self.catalog.get("G03").eligibility["signed_up_within_days"])
        if snapshot.signup_at is None:
            return EligibilityResult(
                goal_id="G03",
                eligible=False,
                measurable=True,
                reasons=["signup_at unknown; insufficient history"],
            )
        age = days_between(snapshot.signup_at, snapshot.decision_time)
        if age > days:
            return EligibilityResult(
                goal_id="G03",
                eligible=False,
                measurable=True,
                reasons=[f"signup age {age:.2f}d exceeds {days}d"],
                evidence_ids=_ids(snapshot.signup_evidence_id),
            )
        return EligibilityResult(
            goal_id="G03",
            eligible=True,
            measurable=True,
            reasons=["signed up within 2 days"],
            evidence_ids=_ids(snapshot.signup_evidence_id),
        )

    def _status_match(self, snapshot: ViewerSnapshot, goal_id: str, allowed: list[str]) -> EligibilityResult | None:
        if snapshot.subscription_status is None:
            return EligibilityResult(
                goal_id=goal_id,
                eligible=False,
                measurable=True,
                reasons=["subscription_status unknown; insufficient history"],
            )
        if snapshot.subscription_status not in allowed:
            return EligibilityResult(
                goal_id=goal_id,
                eligible=False,
                measurable=True,
                reasons=[f"status {snapshot.subscription_status!r} not in {allowed}"],
                evidence_ids=_ids(snapshot.status_evidence_id),
            )
        return None

    def _g04(self, snapshot: ViewerSnapshot) -> EligibilityResult:
        blocked = self._status_match(snapshot, "G04", ["trial_active"])
        if blocked:
            return blocked
        return EligibilityResult(
            goal_id="G04",
            eligible=True,
            measurable=True,
            reasons=["subscription_status is trial_active"],
            evidence_ids=_ids(snapshot.status_evidence_id),
        )

    def _g05(self, snapshot: ViewerSnapshot) -> EligibilityResult:
        if snapshot.has_paid:
            return EligibilityResult(
                goal_id="G05",
                eligible=False,
                measurable=True,
                reasons=["has_paid is true; first paid plan already observed"],
                evidence_ids=_ids(snapshot.paid_evidence_id),
            )
        blocked = self._status_match(snapshot, "G05", ["never"])
        if blocked:
            return blocked
        return EligibilityResult(
            goal_id="G05",
            eligible=True,
            measurable=True,
            reasons=["status never and no paid event"],
            evidence_ids=_ids(snapshot.status_evidence_id),
        )

    def _g06(self, snapshot: ViewerSnapshot) -> EligibilityResult:
        blocked = self._status_match(snapshot, "G06", ["lapsed"])
        if blocked:
            return blocked
        return EligibilityResult(
            goal_id="G06",
            eligible=True,
            measurable=True,
            reasons=["subscription_status is lapsed"],
            evidence_ids=_ids(snapshot.status_evidence_id),
            assumptions=[
                "G06 has no recency window in the catalog; lapsed status is used as written."
            ],
        )

    def _g07(self, snapshot: ViewerSnapshot) -> EligibilityResult:
        idle_days = 3.0
        candidates = [
            series
            for series in snapshot.series
            if series.unfinished
            and days_between(series.last_play_at, snapshot.decision_time) >= idle_days
        ]
        if not candidates:
            return EligibilityResult(
                goal_id="G07",
                eligible=False,
                measurable=True,
                reasons=[
                    "no unfinished show/micro_drama with last play at least 3 days ago"
                ],
                assumptions=[
                    "G07 eligibility is text-only; unfinished series uses episode counts and a 3-day idle."
                ],
            )
        chosen = max(candidates, key=lambda item: as_utc(item.last_play_at))
        return EligibilityResult(
            goal_id="G07",
            eligible=True,
            measurable=True,
            reasons=[f"unfinished series {chosen.content_slug} idle >= 3d"],
            evidence_ids=_ids(chosen.last_play_event_id),
            assumptions=[
                "G07 eligibility is text-only; unfinished series uses episode counts and a 3-day idle."
            ],
        )

    def _g08(self, snapshot: ViewerSnapshot) -> EligibilityResult:
        blocked = self._status_match(snapshot, "G08", ["active"])
        if blocked:
            return blocked
        return EligibilityResult(
            goal_id="G08",
            eligible=True,
            measurable=True,
            reasons=["subscription_status is active"],
            evidence_ids=_ids(snapshot.status_evidence_id),
        )

    def _g09(self, snapshot: ViewerSnapshot) -> EligibilityResult:
        blocked = self._status_match(snapshot, "G09", ["active"])
        if blocked:
            return blocked
        cutoff = as_utc(snapshot.decision_time) - timedelta(days=14)
        days = {
            ist_date(item.received_at)
            for item in snapshot.play_started
            if as_utc(item.received_at) >= cutoff
        }
        if len(days) > 1:
            return EligibilityResult(
                goal_id="G09",
                eligible=False,
                measurable=True,
                reasons=[f"play_started on {len(days)} IST days in last 14d; cap is 1"],
                evidence_ids=_ids(*(item.event_id for item in snapshot.play_started[:3])),
            )
        return EligibilityResult(
            goal_id="G09",
            eligible=True,
            measurable=True,
            reasons=[f"active subscriber with {len(days)} play day(s) in last 14d"],
            evidence_ids=_ids(snapshot.status_evidence_id),
            assumptions=[
                "G09 play-day cap is catalog text; counted from play_started received_at IST dates."
            ],
        )

    def _g12(self, snapshot: ViewerSnapshot) -> EligibilityResult:
        cutoff = as_utc(snapshot.decision_time) - timedelta(days=14)
        prior = [
            item
            for item in snapshot.app_opens
            if item.event_id != snapshot.trigger_event_id
            and as_utc(item.received_at) >= cutoff
        ]
        if prior:
            return EligibilityResult(
                goal_id="G12",
                eligible=False,
                measurable=True,
                reasons=["app_open exists in the 14 days before the trigger"],
                evidence_ids=_ids(prior[0].event_id),
            )
        return EligibilityResult(
            goal_id="G12",
            eligible=True,
            measurable=True,
            reasons=["no prior app_open in the 14 days before the trigger"],
            evidence_ids=_ids(snapshot.trigger_event_id),
        )
