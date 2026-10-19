from __future__ import annotations

import pytest
from pydantic import ValidationError

from oua.schemas import ActionType, Checks, DecisionStatus, ExecutionAck, GoalRef, SuccessSignal
from tests.conftest import CATALOG_VERSION, make_decision, utc


def test_published_cannot_be_no_action():
    with pytest.raises(ValidationError, match="publishable action"):
        make_decision(action=ActionType.NO_ACTION, content=[])


def test_published_requires_execution_success():
    with pytest.raises(ValidationError, match="confirmed executor"):
        make_decision(execution=None)


def test_published_rejects_failed_checks():
    with pytest.raises(ValidationError, match="checks.failed"):
        make_decision(checks=Checks(passed=[], failed=["dialect"]))


def test_published_requires_goal():
    with pytest.raises(ValidationError, match="measurable goal"):
        make_decision(goal=None)


def test_abstained_requires_reason():
    with pytest.raises(ValidationError, match="abstention_reason"):
        make_decision(
            status=DecisionStatus.ABSTAINED,
            action=ActionType.NO_ACTION,
            goal=None,
            content=[],
            execution=None,
            abstention_reason=None,
        )


def test_abstained_rejects_placeholder_reason():
    with pytest.raises(ValidationError, match="placeholder"):
        make_decision(
            status=DecisionStatus.ABSTAINED,
            action=ActionType.NO_ACTION,
            goal=None,
            content=[],
            execution=None,
            abstention_reason="n/a",
        )


def test_abstained_cannot_publish_home_page():
    with pytest.raises(ValidationError, match="NO_ACTION"):
        make_decision(
            status=DecisionStatus.ABSTAINED,
            action=ActionType.HOME_PAGE,
            execution=None,
            abstention_reason="Holdout arm must receive no agent action.",
        )


def test_failed_safe_requires_failure_reason():
    with pytest.raises(ValidationError, match="failure_reason"):
        make_decision(
            status=DecisionStatus.FAILED_SAFE,
            action=ActionType.NO_ACTION,
            content=[],
            execution=None,
            failure_reason=None,
        )


def test_failed_safe_cannot_succeed_execution():
    with pytest.raises(ValidationError, match="execution.confirmed"):
        make_decision(
            status=DecisionStatus.FAILED_SAFE,
            action=ActionType.NO_ACTION,
            content=[],
            failure_reason="model_provider HTTP 503",
            execution=ExecutionAck(confirmed=True, executor="test_harness"),
        )


def test_placeholder_assumption_rejected():
    with pytest.raises(ValidationError, match="placeholder"):
        make_decision(
            status=DecisionStatus.ABSTAINED,
            action=ActionType.NO_ACTION,
            goal=None,
            content=[],
            execution=None,
            abstention_reason="No facts persisted yet; ingestion is not implemented.",
            evidence=[],
            evidence_details=[],
            assumptions=["n/a"],
        )


def test_catalog_unknown_and_unmeasurable(catalog):
    with pytest.raises(KeyError):
        catalog.get("G99")
    with pytest.raises(ValueError, match="no supported measurable"):
        catalog.require_measurable(
            GoalRef(
                id="G10",
                deadline=utc("2026-10-08T00:00:00.000Z"),
                success_signal=SuccessSignal(
                    event="play_started",
                    condition="proxy",
                    window_hours=168,
                ),
                rule_version=CATALOG_VERSION,
            )
        )
    with pytest.raises(ValidationError):
        SuccessSignal(event="tv_app_open", condition="any", window_hours=336)


def test_status_enum_values():
    assert DecisionStatus.PUBLISHED.value == "PUBLISHED"
    assert DecisionStatus.ABSTAINED.value == "ABSTAINED"
    assert DecisionStatus.FAILED_SAFE.value == "FAILED_SAFE"
