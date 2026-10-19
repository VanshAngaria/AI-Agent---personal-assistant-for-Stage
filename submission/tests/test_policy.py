"""Behaviour comes from config (goals.json + agent_policy.json), not from code."""

import json

import pytest

from oua.policy import Policy
from tests.conftest import at, trigger


def _policy(**changes) -> Policy:
    data = Policy.load().model_dump()
    for path, value in changes.items():
        node = data
        keys = path.split(".")
        for k in keys[:-1]:
            node = node[k]
        node[keys[-1]] = value
    return Policy.model_validate(data)


def test_stale_threshold_is_policy_not_code(mini, build):
    mini.viewer("U-S", signup=at(days=-1))
    t = trigger("U-S", at(), buffered_s=1200)  # waited 20 min on the phone
    default = build(mini).handle_trigger(t)["decision"]
    assert default["no_action_kind"] == "stale_trigger"
    relaxed = build(mini, policy=_policy(**{"trigger.stale_after_s": 3600})).handle_trigger(dict(t))["decision"]
    assert relaxed["status"] == "PUBLISHED" and relaxed["trigger"]["live_visit"] is True
    assert relaxed["policy_version"] == Policy.load().version


def test_a_new_goal_needs_config_only(mini, build):
    mini.goals["goals"].append({
        "goal_id": "G13", "name": "first_search_play_7d", "description": "test-only goal",
        "eligibility": {"text": "Never paid", "subscription_status": ["never"]},
        "success": {"event": "play_started", "condition": "any title", "window_hours": 168},
    })
    mini.viewer("U-NEW13", status="never")
    policy = _policy(goal_priority=["G13"] + Policy.load().goal_priority)
    svc = build(mini, policy=policy)
    d = svc.handle_trigger(trigger("U-NEW13", at()))["decision"]
    assert d["goal"]["id"] == "G13" and d["goal"]["success_signal"]["window_hours"] == 168
    ev = next(e for e in d["goal_evaluations"] if e["goal_id"] == "G13")
    assert ev["eligible"] and ev["checks"] == ["subscription_status_in"]


def test_text_only_goal_without_reviewed_interpretation_is_refused(mini, build):
    mini.goals["goals"].append({"goal_id": "G14", "name": "vague", "eligibility": {"text": "Viewers who seem keen."},
                                "success": {"event": "play_started", "condition": "any", "window_hours": 24}})
    mini.viewer("U-V", status="never")
    d = build(mini).handle_trigger(trigger("U-V", at()))["decision"]
    ev = next(e for e in d["goal_evaluations"] if e["goal_id"] == "G14")
    assert not ev["eligible"] and "no reviewed interpretation" in ev["reason"]


def test_interpretations_can_be_changed_in_policy(mini, build):
    mini.viewer("U-LAP", status="lapsed")
    mini.event("U-LAP", "subscription_lapsed", at(days=-40))
    strict = build(mini).handle_trigger(trigger("U-LAP", at()))["decision"]
    assert not next(e for e in strict["goal_evaluations"] if e["goal_id"] == "G06")["eligible"]
    data = Policy.load().model_dump()
    data["goal_interpretations"]["G06"]["extra_checks"][0]["within_days"] = 60
    loose = build(mini, policy=Policy.model_validate(data)).handle_trigger(trigger("U-LAP", at()))["decision"]
    assert next(e for e in loose["goal_evaluations"] if e["goal_id"] == "G06")["eligible"]


def test_bad_policy_fails_at_load(tmp_path):
    data = json.loads(json.dumps(Policy.load().model_dump()))
    data["goal_interpretations"]["G07"]["extra_checks"] = [{"check": "vibes"}]
    with pytest.raises(ValueError, match="unknown eligibility check"):
        Policy.model_validate(data)
    data = json.loads(json.dumps(Policy.load().model_dump()))
    data["success_matchers"]["G03"] = [{"match": "feels_like_success"}]
    with pytest.raises(ValueError, match="unknown success matcher"):
        Policy.model_validate(data)
