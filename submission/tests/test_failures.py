"""Brief section 8: tool / model failures degrade to a smaller safe action or no action, on time."""

from tests.conftest import at, trigger


def _window(dep, kind, start, until):
    return {"at": start, "type": f"dependency_{kind}", "dependency": dep, "until": until, "detail": "test outage"}


def _new(mini, uid):
    mini.viewer(uid, signup=at(days=-1))  # several eligible goals -> the model is consulted


def test_model_outage_falls_back_to_rules_and_opens_the_circuit(mini, build):
    _new(mini, "U-A")
    _new(mini, "U-B")
    svc = build(mini)
    svc.on_control(_window("model_provider", "unavailable", at(minutes=-1), at(minutes=10)))
    d1 = svc.handle_trigger(trigger("U-A", at()))["decision"]
    d2 = svc.handle_trigger(trigger("U-B", at(seconds=20)))["decision"]
    assert d1["model"]["outcome"] == "unavailable" and d1["status"] == "PUBLISHED" and d1["goal"]["id"] == "G01"
    assert d2["model"]["outcome"] == "unavailable" and "circuit" in d2["model"]["detail"]
    assert d2["budget"]["model_calls"] == 0  # breaker open: no wasted call
    assert d2["status"] == "PUBLISHED"


def test_model_timeout_is_cancelled_within_budget(mini, build):
    _new(mini, "U-T")
    t = trigger("U-T", at())
    mini.faults.append({"trigger_event_id": t["event_id"], "user_id": "U-T", "fault": "timeout", "seconds": 45})
    svc = build(mini)
    d = svc.handle_trigger(t)["decision"]
    assert d["model"]["outcome"] == "timeout"
    assert d["latency_ms"] < svc.s.page_target_s * 1000  # 45 s fault cancelled at the model timeout
    later = [t for t in d["llm"]["tasks"] if t["task"] != "choose_goal"]
    assert later and all("already timed out" in t["detail"] for t in later)  # no second wait on a failing model
    assert d["status"] == "PUBLISHED"


def test_malformed_model_output_is_ignored(mini, build):
    _new(mini, "U-M")
    t = trigger("U-M", at())
    mini.faults.append({"trigger_event_id": t["event_id"], "user_id": "U-M", "fault": "malformed_output",
                        "model_output": "{\"goal_id\": \"G02\", \"eligible\": tr"})
    svc = build(mini)
    d = svc.handle_trigger(t)["decision"]
    assert d["model"]["outcome"] == "malformed"
    assert d["goal"]["id"] == "G01" and d["status"] == "PUBLISHED"


def test_data_store_outage_without_snapshot_fails_safe(mini, build):
    _new(mini, "U-S")
    svc = build(mini)
    svc.on_control(_window("user_data_store", "unavailable", at(minutes=-1), at(minutes=10)))
    res = svc.handle_trigger(trigger("U-S", at()))
    d = res["decision"]
    assert d["status"] == "FAILED_SAFE" and d["no_action_kind"] == "data_store"
    assert res["actions"] == [] and d["latency_ms"] < 60_000


def test_data_store_degraded_uses_cached_snapshot_and_skips_messages(mini, build):
    _new(mini, "U-C")
    svc = build(mini)
    svc.handle_trigger(trigger("U-C", at()))  # warms this agent's snapshot of the viewer
    svc.on_control(_window("user_data_store", "unavailable", at(hours=3), at(hours=4)))
    d = svc.handle_trigger(trigger("U-C", at(hours=3, minutes=5)))["decision"]
    assert any("cached" in a for a in d["assumptions"])
    assert d["status"] == "PUBLISHED" and d["action"] == "HOME_PAGE"
    assert not [a for a in d["actions"] if a["type"] in ("push", "whatsapp")]
    assert d["latency_ms"] < 60_000


def test_tool_budget_exhaustion_fails_closed(mini, build):
    _new(mini, "U-BUD")
    svc = build(mini, max_tool_calls=2)
    d = svc.handle_trigger(trigger("U-BUD", at()))["decision"]
    assert d["status"] == "FAILED_SAFE" and d["no_action_kind"] == "budget"
    assert d["budget"]["tool_calls"] <= 2
