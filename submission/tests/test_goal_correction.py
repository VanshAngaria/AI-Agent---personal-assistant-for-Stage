"""Brief section 8: a goal's success condition is corrected mid-replay."""

import pytest

from tests.conftest import at, trigger

# Same shape as replay/control_events.jsonl: note there is no window_hours in new_success.
CORRECTION = {"at": None, "type": "goal_rule_corrected", "goal_id": "G03",
              "old_success": {"event": "app_open", "condition": "on a later IST calendar day within the window"},
              "new_success": {"event": "play_started", "condition": "on a later IST calendar day within the window"},
              "applies_to": "every G03 goal that is still open, including goals opened before this correction"}


def _g03_viewer(mini, uid):
    # New viewer who already played and completed: G01/G02 are not eligible, G03 (2-day signup) is first in line.
    mini.viewer(uid, signup=at(days=-1))
    mini.event(uid, "play_started", at(hours=-20), {"title_id": "T0101", "episode_id": "micro-1-d1-s1e1"})
    mini.event(uid, "episode_completed", at(hours=-19), {"title_id": "T0101", "episode_id": "micro-1-d1-s1e1"})


def test_correction_updates_only_open_plans_of_that_goal(mini, build):
    _g03_viewer(mini, "U-OPEN")
    _g03_viewer(mini, "U-CLOSED")
    mini.viewer("U-OTHER", signup=at(days=-1))  # gets G01
    svc = build(mini)
    for uid in ("U-OPEN", "U-CLOSED", "U-OTHER"):
        svc.handle_trigger(trigger(uid, at()))
    assert svc.store.open_plan("U-OPEN")["goal_id"] == "G03"
    # U-CLOSED succeeds under the old rule (app open on the next IST day) before the correction.
    svc.on_live_event({**trigger("U-CLOSED", at(hours=14)), "deliver_at": at(hours=14)})
    closed = svc.store.plans_for("U-CLOSED")[0]
    assert closed["status"] == "succeeded"

    affected = svc.on_control(dict(CORRECTION, at=at(hours=15)))

    open_plan = svc.store.open_plan("U-OPEN")
    assert affected["open_plans_updated"] == [open_plan["plan_id"]]
    assert affected["other_viewers_reprocessed"] == 0
    assert open_plan["success_event"] == "play_started" and open_plan["rule_version"].endswith(".r2")
    assert open_plan["window_hours"] == 48  # inherited: the correction did not restate it
    other = svc.store.open_plan("U-OTHER")
    assert other["goal_id"] == "G01" and other["rule_version"] == "G01@2026-09-30"
    assert not [h for h in svc.store.plan_history("U-OTHER") if h["change"] == "rule_corrected"]
    after = svc.store.plans_for("U-CLOSED")[0]
    assert after["status"] == "succeeded" and after["success_event"] == "app_open"  # closed outcome untouched
    hist = [h for h in svc.store.plan_history("U-OPEN") if h["change"] == "rule_corrected"]
    assert hist and hist[0]["detail"]["old_success_event"] == "app_open"


def test_corrected_rule_is_used_for_new_goals_and_for_judging(mini, build):
    _g03_viewer(mini, "U-LATE")
    svc = build(mini)
    svc.on_control(dict(CORRECTION, at=at(minutes=-5)))
    d = svc.handle_trigger(trigger("U-LATE", at()))["decision"]
    assert d["goal"]["id"] == "G03" and d["goal"]["success_signal"]["event"] == "play_started"
    plan = svc.store.open_plan("U-LATE")
    next_day_open = {"event_id": "x1", "event": "app_open", "received_at": at(hours=14), "properties": {}}
    next_day_play = {"event_id": "x2", "event": "play_started", "received_at": at(hours=14),
                     "properties": {"title_id": "T0102"}}
    same_day_play = {"event_id": "x3", "event": "play_started", "received_at": at(hours=2), "properties": {"title_id": "T0102"}}
    assert svc.judge.judge_window(plan, [next_day_open])[0] is False
    assert svc.judge.judge_window(plan, [same_day_play])[0] is False
    assert svc.judge.judge_window(plan, [next_day_play])[0] is True


def test_corrections_and_withdrawals_survive_a_restart(mini, build):
    from oua.agent import AgentService
    from oua.pack import Pack

    _g03_viewer(mini, "U-RESTART")
    svc = build(mini)
    svc.on_control(dict(CORRECTION, at=at(minutes=-5)))
    svc.on_control({"at": at(minutes=-5), "type": "title_withdrawn", "title_id": "T0101", "reason": "test"})
    restarted = AgentService(svc.s, svc.store, Pack.open(svc.pack.root))  # same durable state, fresh process
    assert restarted.rules.current("G03").success["event"] == "play_started"
    assert restarted.rules.current("G03").version == svc.rules.current("G03").version
    d = restarted.handle_trigger(trigger("U-RESTART", at()))["decision"]
    assert d["goal"]["success_signal"]["event"] == "play_started"
    assert "title:T0101" not in d["content"]


def test_correction_cannot_make_a_goal_unmeasurable(mini, build):
    svc = build(mini)
    with pytest.raises(ValueError):
        svc.rules.correct_success("G03", {"event": None, "condition": "engages"}, at(), "test")
