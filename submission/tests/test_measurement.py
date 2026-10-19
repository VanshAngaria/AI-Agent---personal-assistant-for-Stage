"""Closing the loop: attributable outcomes, live plan revision, lessons, and the impact maths."""

import json

import pytest

from oua.measure import _attributable, _diff, impact_report, judge_all, judge_plan
from tests.conftest import at, trigger


def _engaged_never_payer(mini, uid, arm="agent"):
    """Status never, already plays and completes, recently active: only G05 (first paid plan) is eligible."""
    mini.viewer(uid, arm=arm, status="never")
    mini.event(uid, "app_open", at(days=-2))
    mini.event(uid, "play_started", at(days=-2, minutes=1), {"title_id": "T0111", "episode_id": "film-11-d1-s1e1"})
    mini.event(uid, "episode_completed", at(days=-2, minutes=90), {"title_id": "T0111", "episode_id": "film-11-d1-s1e1"})


def _purchase(uid, received, buffered_s=0.0):
    return {**trigger(uid, received, buffered_s=buffered_s, name="purchase_completed"),
            "properties": {"plan_id": "P_MONTH", "price_inr": 149, "is_trial": False}}


def test_purchase_two_minutes_after_decision_closes_the_plan(mini, build):
    _engaged_never_payer(mini, "U-BUY")
    svc = build(mini)
    d = svc.handle_trigger(trigger("U-BUY", at()))["decision"]
    assert d["goal"]["id"] == "G05"
    r = svc.on_live_event(_purchase("U-BUY", at(minutes=2)))
    assert r["plan_closed"] == "succeeded"
    assert svc.store.open_plan("U-BUY") is None
    assert any(l["kind"] == "goal_succeeded:G05" for l in svc.store.lessons("U-BUY"))
    # The next open re-plans from the new facts instead of pursuing a finished goal.
    d2 = svc.handle_trigger(trigger("U-BUY", at(hours=3)))["decision"]
    assert d2["goal"] is None or d2["goal"]["id"] != "G05"


def test_purchase_that_happened_before_the_goal_is_not_credited(mini, build):
    _engaged_never_payer(mini, "U-LATEBUY")
    svc = build(mini)
    svc.handle_trigger(trigger("U-LATEBUY", at()))
    r = svc.on_live_event(_purchase("U-LATEBUY", at(minutes=2), buffered_s=3 * 3600))  # made offline 3h earlier
    assert r["plan_closed"] == "inconclusive"
    assert "before the goal started" in svc.store.plans_for("U-LATEBUY")[0]["close_reason"]


def test_attribution_uses_device_buffer_not_device_clock():
    e = {"received_at": at(minutes=10), "occurred_at": at(hours=-6), "sent_at": at(hours=-6, seconds=2)}  # clock skew only
    assert _attributable(e, at()) is True
    buffered = {"received_at": at(minutes=10), "occurred_at": at(hours=-2), "sent_at": at(minutes=10)}
    assert _attributable(buffered, at()) is False


def test_judge_and_impact_end_to_end(mini, build, tmp_path):
    for i in range(4):
        _engaged_never_payer(mini, f"U-A{i}", arm="agent")
        _engaged_never_payer(mini, f"U-H{i}", arm="holdout")
    svc = build(mini)
    for i in range(4):
        svc.handle_trigger(trigger(f"U-A{i}", at(seconds=i)))
        svc.handle_trigger(trigger(f"U-H{i}", at(seconds=10 + i)))
    out = tmp_path / "run"
    (out / "sim").mkdir(parents=True)
    # Simulated outcomes: one agent viewer and one holdout viewer buy inside the window; one agent purchase is late.
    sim = [_purchase("U-A0", at(days=2)), _purchase("U-H0", at(days=3)), _purchase("U-A1", at(days=30)),
           _purchase("U-A3", at(days=1), buffered_s=2 * 86400)]  # uploaded inside the window, made before the goal
    for e in sim:
        e.pop("deliver_at")
    (out / "sim" / "outcome_events.jsonl").write_text("\n".join(json.dumps(e) for e in sim), encoding="utf-8")
    res = judge_all(svc.store, svc.pack, out)
    assert res["plans"] == 8
    by_viewer = {o["viewer_id"]: o for o in svc.store.outcomes()}
    assert by_viewer["U-A0"]["status"] == "succeeded" and by_viewer["U-H0"]["status"] == "succeeded"
    assert by_viewer["U-A1"]["status"] == "failed"  # bought, but 30 days later: outside the 7-day window
    assert by_viewer["U-A2"]["status"] == "failed"
    assert by_viewer["U-A3"]["status"] == "failed" and "late upload" in by_viewer["U-A3"]["reason"]
    # Had the observed data ended before the deadline, the same plan would be inconclusive, not failed.
    plan = svc.store.plans_for("U-A2")[0]
    assert judge_plan(svc.store, svc.pack, plan, observation_end=at(days=3))["status"] == "inconclusive"
    assert any(l["kind"] == "goal_succeeded:G05" for l in svc.store.lessons("U-A0"))
    imp = impact_report(svc.store, svc.pack, out)
    assert imp["arms"]["agent"]["succeeded"] == 1 and imp["arms"]["holdout"]["succeeded"] == 1
    assert imp["primary"]["diff"] == pytest.approx(0.0)
    assert (out / "impact_report.md").exists()


def test_two_proportion_difference():
    r = _diff(30, 100, 20, 100)
    assert r["diff"] == pytest.approx(0.10)
    assert r["ci95"][0] == pytest.approx(0.10 - 1.96 * ((0.3 * 0.7 + 0.2 * 0.8) / 100) ** 0.5)
    assert 0.09 < r["p_value"] < 0.11
