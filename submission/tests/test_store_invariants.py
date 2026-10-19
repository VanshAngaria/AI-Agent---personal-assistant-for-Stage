from __future__ import annotations

import sqlite3

import pytest

from oua.schemas import ExperimentArm
from oua.state.store import DuplicateActionError, UniqueActivePlanError
from tests.conftest import TEST_VIEWER, make_decision


def test_duplicate_action_prevention(store):
    store.save_decision(make_decision())
    payload = {"type": "home_page"}
    store.save_action("A-1", "D-TEST-1", TEST_VIEWER, "evt-test-1", "home_page", payload)
    with pytest.raises(DuplicateActionError):
        store.save_action("A-1", "D-TEST-1", TEST_VIEWER, "evt-test-1", "home_page", payload)
    with pytest.raises(DuplicateActionError):
        store.save_action("A-2", "D-TEST-1", TEST_VIEWER, "evt-test-1", "home_page", payload)


def test_one_active_plan_per_viewer(store):
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
        )
    count = store.conn.execute(
        "SELECT COUNT(*) AS n FROM active_plans WHERE viewer_id = ?",
        (TEST_VIEWER,),
    ).fetchone()
    assert count["n"] == 1


def test_holdout_assignment_constraint(store):
    store.set_assignment(TEST_VIEWER, ExperimentArm.HOLDOUT)
    assert store.get_assignment(TEST_VIEWER) == "holdout"
    with pytest.raises(ValueError):
        store.set_assignment("TEST-VIEWER-002", "treatment")
    with pytest.raises(sqlite3.IntegrityError):
        store.conn.execute(
            "INSERT INTO experiment_assignments (viewer_id, arm, assigned_at) VALUES ('TEST-X','served','x')"
        )
        store.conn.commit()
