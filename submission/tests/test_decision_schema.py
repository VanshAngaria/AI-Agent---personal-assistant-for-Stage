from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from oua.schemas import (
    ActionType,
    Checks,
    Decision,
    DecisionStatus,
    ExecutionAck,
    GoalRef,
    SuccessSignal,
)
from tests.conftest import CATALOG_VERSION, make_decision, utc


def test_published_round_trip_json():
    decision = make_decision()
    payload = decision.to_runtime_dict()
    assert payload["decision_id"] == "D-TEST-1"
    assert payload["status"] == "PUBLISHED"
    assert payload["goal"]["success_signal"]["event"] == "play_started"
    clone = Decision.model_validate_json(json.dumps(payload))
    assert clone.decision_id == decision.decision_id


def test_abstained_requires_reason_and_no_action():
    decision = make_decision(
        status=DecisionStatus.ABSTAINED,
        action=ActionType.NO_ACTION,
        goal=None,
        content=[],
        execution=None,
        abstention_reason="No measurable eligible goal in catalog for this fixture viewer.",
        failure_reason=None,
        checks=Checks(passed=["holdout_not_applicable"], failed=[]),
        evidence=[],
        evidence_details=[],
        assumptions=[],
    )
    assert decision.status is DecisionStatus.ABSTAINED
    assert decision.goal is None


def test_failed_safe_requires_failure_reason():
    decision = make_decision(
        status=DecisionStatus.FAILED_SAFE,
        action=ActionType.NO_ACTION,
        content=[],
        execution=ExecutionAck(confirmed=False, executor="model_provider"),
        failure_reason="model_provider returned HTTP 503 inside the configured outage window.",
        abstention_reason=None,
        checks=Checks(passed=[], failed=["model_provider_available"]),
    )
    assert decision.status is DecisionStatus.FAILED_SAFE


def test_published_without_executor_ack_is_invalid():
    with pytest.raises(ValidationError):
        make_decision(execution=None)


def test_published_with_no_action_is_invalid():
    with pytest.raises(ValidationError):
        make_decision(action=ActionType.NO_ACTION, content=[])


def test_published_without_goal_is_invalid():
    with pytest.raises(ValidationError):
        make_decision(goal=None)


def test_published_with_failed_checks_is_invalid():
    with pytest.raises(ValidationError):
        make_decision(checks=Checks(passed=[], failed=["dialect"]))


def test_abstained_without_reason_is_invalid():
    with pytest.raises(ValidationError):
        make_decision(
            status=DecisionStatus.ABSTAINED,
            action=ActionType.NO_ACTION,
            content=[],
            execution=None,
            abstention_reason="  ",
            goal=None,
        )


def test_failed_safe_cannot_confirm_execution():
    with pytest.raises(ValidationError):
        make_decision(
            status=DecisionStatus.FAILED_SAFE,
            action=ActionType.NO_ACTION,
            content=[],
            failure_reason="timeout",
            execution=ExecutionAck(confirmed=True, executor="simulator"),
        )


def test_evidence_ids_must_resolve():
    with pytest.raises(ValidationError):
        make_decision(evidence=["event:missing"])


def test_empty_evidence_is_allowed_when_honest():
    decision = make_decision(
        status=DecisionStatus.ABSTAINED,
        action=ActionType.NO_ACTION,
        goal=None,
        content=[],
        evidence=[],
        evidence_details=[],
        assumptions=[],
        execution=None,
        abstention_reason="No facts persisted yet; ingestion is not implemented.",
    )
    assert decision.evidence == []
    assert decision.evidence_details == []


def test_blank_confidence_reason_rejected():
    with pytest.raises(ValidationError):
        make_decision(confidence_reason="   ")


def test_invented_success_event_rejected_by_signal_model():
    with pytest.raises(ValidationError):
        SuccessSignal(event="tv_app_open", condition="any", window_hours=336)
    with pytest.raises(ValidationError):
        SuccessSignal(event="", condition="engages meaningfully", window_hours=168)


def test_cannot_proxy_g10_with_another_event(catalog):
    proxied = GoalRef(
        id="G10",
        deadline=utc("2026-10-08T00:00:00.000Z"),
        success_signal=SuccessSignal(
            event="play_started",
            condition="engages meaningfully",
            window_hours=168,
        ),
        rule_version=CATALOG_VERSION,
    )
    with pytest.raises(ValueError, match="no supported measurable success signal"):
        catalog.require_measurable(proxied)


def test_catalog_rejects_unmeasurable_and_unknown_goals(catalog):
    assert catalog.get("G10").is_measurable is False
    assert catalog.get("G11").is_measurable is False
    with pytest.raises(ValueError):
        catalog.success_signal_for("G10")
    with pytest.raises(KeyError):
        catalog.get("G99")

    published = make_decision()
    catalog.require_measurable(published.goal)

    bad = make_decision(
        goal=GoalRef(
            id="G01",
            deadline=utc("2026-10-02T12:00:00.000Z"),
            success_signal=SuccessSignal(
                event="play_started",
                condition="any title",
                window_hours=24,
            ),
            rule_version="not-a-catalog-version",
        )
    )
    with pytest.raises(ValueError):
        catalog.require_measurable(bad.goal)
