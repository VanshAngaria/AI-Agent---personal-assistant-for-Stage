from __future__ import annotations

import pytest

from oua.schemas import ExperimentArm
from oua.state.store import DuplicateActionError, UniqueActivePlanError
from tests.conftest import TEST_VIEWER, make_decision


def test_decision_round_trip(store, service):
    decision = make_decision()
    store.add_fact(
        TEST_VIEWER,
        "subscription_status",
        {"value": "unknown"},
        source="test_fixture",
        source_id=None,
        at=None,
    )
    service.persist(decision)
    loaded = service.load("D-TEST-1")
    assert loaded is not None
    assert loaded.viewer_id == TEST_VIEWER
    snapshot = store.get_snapshot("D-TEST-1")
    assert snapshot is not None
    assert snapshot[0]["fact_key"] == "subscription_status"
    assert snapshot[0]["source"] == "test_fixture"


def test_catalog_checked_on_persist(service):
    decision = make_decision()
    decision.goal.rule_version = "wrong"
    with pytest.raises(ValueError):
        service.persist(decision)


def test_trigger_idempotency(store):
    first = store.claim_trigger("evt-test-1", TEST_VIEWER, "2026-10-01T12:00:00.000Z")
    second = store.claim_trigger("evt-test-1", TEST_VIEWER, "2026-10-01T12:05:00.000Z")
    assert first is True
    assert second is False
    store.save_decision(make_decision())
    assert store.get_decision_id_for_trigger("evt-test-1") == "D-TEST-1"


def test_duplicate_decision_for_same_trigger_rejected(store):
    store.save_decision(make_decision())
    other = make_decision(decision_id="D-TEST-2")
    with pytest.raises(ValueError):
        store.save_decision(other)


def test_assignment_persisted(store):
    store.set_assignment(TEST_VIEWER, ExperimentArm.HOLDOUT)
    assert store.get_assignment(TEST_VIEWER) == "holdout"


def test_goal_rule_version_and_outcome(store, catalog):
    rule = catalog.get("G01")
    store.register_goal_rule("G01", catalog.version, rule.model_dump(), "2026-09-30T00:00:00.000Z")
    store.save_outcome(
        viewer_id=TEST_VIEWER,
        goal_id="G01",
        rule_version=catalog.version,
        started_at="2026-10-01T12:00:00.000Z",
        status="inconclusive",
        signal_event_id=None,
        judged_at="2026-10-01T12:00:02.000Z",
    )
    row = store.conn.execute("SELECT status FROM outcomes WHERE viewer_id = ?", (TEST_VIEWER,)).fetchone()
    assert row["status"] == "inconclusive"


def test_duplicate_action_prevented(store):
    store.save_decision(make_decision())
    payload = {"type": "home_page"}
    store.save_action("A-1", "D-TEST-1", TEST_VIEWER, "evt-test-1", "home_page", payload)
    with pytest.raises(DuplicateActionError):
        store.save_action("A-2", "D-TEST-1", TEST_VIEWER, "evt-test-1", "home_page", payload)
    with pytest.raises(DuplicateActionError):
        store.save_action("A-1", "D-TEST-1", TEST_VIEWER, "evt-test-1", "push", {"type": "push"})


def test_active_plan_invariant(store):
    store.set_active_plan(
        viewer_id=TEST_VIEWER,
        goal_id="G01",
        deadline="2026-10-02T12:00:00.000Z",
        success_event="play_started",
        success_condition="any title",
        window_hours=24,
        rule_version="2026-09-30",
        started_at="2026-10-01T12:00:00.000Z",
        decision_id="D-TEST-1",
        replace_existing=False,
    )
    with pytest.raises(UniqueActivePlanError):
        store.set_active_plan(
            viewer_id=TEST_VIEWER,
            goal_id="G02",
            deadline="2026-10-04T12:00:00.000Z",
            success_event="episode_completed",
            success_condition="any episode",
            window_hours=72,
            rule_version="2026-09-30",
            started_at="2026-10-01T12:01:00.000Z",
            decision_id="D-TEST-2",
            replace_existing=False,
        )
    store.set_active_plan(
        viewer_id=TEST_VIEWER,
        goal_id="G02",
        deadline="2026-10-04T12:00:00.000Z",
        success_event="episode_completed",
        success_condition="any episode",
        window_hours=72,
        rule_version="2026-09-30",
        started_at="2026-10-01T12:01:00.000Z",
        decision_id="D-TEST-2",
        replace_existing=True,
    )
    plan = store.get_active_plan(TEST_VIEWER)
    assert plan is not None
    assert plan["goal_id"] == "G02"
    count = store.conn.execute("SELECT COUNT(*) AS n FROM active_plans WHERE viewer_id = ?", (TEST_VIEWER,)).fetchone()
    assert count["n"] == 1
