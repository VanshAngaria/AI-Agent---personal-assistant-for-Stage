"""Real-time behaviour: replay ordering, bursts, per-viewer serialisation, output files, decision schema."""

import json

import pytest
from pydantic import ValidationError

from oua.harness import run_replay
from oua.schemas import Decision
from tests.conftest import at, trigger


def test_burst_of_opens_queues_but_stays_inside_the_budget(mini, build, tmp_path):
    n = 300
    for i in range(n):
        mini.viewer(f"U-P{i:03d}", arm="agent" if i % 2 else "holdout", signup=at(days=-1))
        mini.triggers.append(trigger(f"U-P{i:03d}", at(seconds=i // 30)))  # 300 opens inside ~10 seconds
    dup = dict(mini.triggers[5])
    mini.triggers.append(dict(dup, received_at=at(seconds=20), deliver_at=at(seconds=20)))
    svc = build(mini, workers=2)
    out = tmp_path / "run"
    summary = run_replay(svc, svc.pack, out, workers=2, log=lambda m: None)
    assert summary["decisions"] == n and summary["redeliveries"] == 1
    rep = json.loads((out / "latency_report.json").read_text())
    assert rep["queue_wait_s"]["max"] > 0  # contention is visible, not hidden
    assert rep["over_budget_60s"] == 0 and rep["over_failure_300s"] == 0
    decisions = [json.loads(x) for x in (out / "decisions.jsonl").read_text().splitlines()]
    for d in decisions:
        Decision.model_validate(d)
    actions = [json.loads(x) for x in (out / "actions.jsonl").read_text().splitlines()]
    arms = {u: a for u, a in mini.arms.items()}
    assert actions and all(arms[a["user_id"]] == "agent" for a in actions)
    assert {"action_id", "decision_id", "user_id", "type", "published_at"} <= set(actions[0])


def test_one_viewer_is_never_decided_concurrently(mini, build, tmp_path):
    mini.viewer("U-SAME", signup=at(days=-1))
    mini.triggers += [trigger("U-SAME", at()), trigger("U-SAME", at())]
    svc = build(mini, workers=8)
    run_replay(svc, svc.pack, tmp_path / "run", workers=8, log=lambda m: None)
    d1, d2 = svc.store.decisions_for("U-SAME")
    finished_first = d1["trigger"]["processing_started_at"] < d2["trigger"]["processing_started_at"]
    assert finished_first and d2["queue_wait_ms"] >= d1["decided_in_ms"] - 1
    assert len([p for p in svc.store.plans_for("U-SAME") if p["status"] == "open"]) == 1


def test_control_events_apply_in_time_order(mini, build, tmp_path):
    mini.viewer("U-W", signup=at(days=-1))
    mini.triggers.append(trigger("U-W", at(minutes=-30)))
    mini.controls.append({"at": at(), "type": "title_withdrawn", "title_id": "T0101", "reason": "licence ended"})
    mini.triggers.append(trigger("U-W", at(hours=3)))
    svc = build(mini)
    run_replay(svc, svc.pack, tmp_path / "run", workers=2, log=lambda m: None)
    before, *rest = svc.store.decisions_for("U-W")
    repair = [d for d in rest if d["trigger"]["event"] == "control:title_withdrawn"]
    later = [d for d in rest if d["trigger"]["event"] == "app_open"][0]
    assert "title:T0101" in before["content"]
    assert repair and repair[0]["status"] == "PUBLISHED"  # live page repaired at withdrawal time
    assert "title:T0101" not in repair[0]["content"] and repair[0]["published_at"] == at()
    assert "title:T0101" not in later["content"]
    old_page = svc.store.actions_for("U-W")[0]
    assert old_page["superseded_by"] == repair[0]["actions"][0]["action_id"]


def _valid() -> dict:
    return {"schema_version": "2.0.0", "decision_id": "D-1", "viewer_id": "U-1", "arm": "agent",
            "trigger": {"event": "app_open", "event_id": "e", "received_at": "x", "deliver_at": "x", "event_age_s": 1,
                        "buffered_s": 0, "live_visit": True},
            "goal": {"id": "G01", "name": "first_play_24h", "deadline": "x", "success_signal": {}, "rule_version": "v",
                     "started_at": "x"},
            "action": "HOME_PAGE", "actions": [{"action_id": "A-1", "type": "home_page"}], "dialect": {"used": "d1", "source": "app_setting"},
            "content": ["title:T1"], "confidence": 0.6, "confidence_reason": "Heuristic: fresh profile", "evidence": ["e1"],
            "evidence_details": [{"id": "e1", "source": "event"}], "checks": {"passed": [], "failed": []}, "model": {},
            "budget": {}, "decided_in_ms": 5, "queue_wait_ms": 0, "latency_ms": 5, "published_at": "x", "status": "PUBLISHED"}


@pytest.mark.parametrize("patch", [
    {"actions": []},                                            # published without an action
    {"goal": None},                                             # published without a goal
    {"arm": "holdout"},                                         # published to a holdout viewer
    {"evidence": ["e1", "e2"]},                                 # cites evidence it cannot resolve
    {"status": "NO_ACTION", "action": "NO_ACTION", "actions": [], "content": []},  # no action without a reason
    {"confidence_reason": "0.6"},                               # a number alone is not a reason
])
def test_decision_schema_rejects_invalid_records(patch):
    Decision.model_validate(_valid())
    with pytest.raises(ValidationError):
        Decision.model_validate({**_valid(), **patch})
