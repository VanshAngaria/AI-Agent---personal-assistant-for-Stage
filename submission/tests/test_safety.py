"""Verification and safety: holdout, injected instructions, unsupported model claims, messaging policy."""

from datetime import timedelta

import pytest

from oua.planner import MessageProposal, PageProposal
from oua.timeutil import ist_at
from tests.conftest import T0, at, trigger


def test_holdout_gets_a_shadow_goal_and_no_action(mini, build):
    mini.viewer("U-HOLD", arm="holdout", signup=at(days=-1))
    svc = build(mini)
    res = svc.handle_trigger(trigger("U-HOLD", at()))
    d = res["decision"]
    assert d["status"] == "NO_ACTION" and d["no_action_kind"] == "holdout"
    assert d["goal"]["id"] == "G01"  # same goal the agent arm would get, for measurement
    assert res["actions"] == [] and svc.store.actions_for("U-HOLD") == []
    assert svc.store.open_plan("U-HOLD")["arm"] == "holdout"


def test_unassigned_viewer_fails_closed(mini, build):
    svc = build(mini)
    d = svc.handle_trigger(trigger("U-NOBODY", at()))["decision"]
    assert d["status"] == "FAILED_SAFE" and d["no_action_kind"] == "no_arm"


def test_injected_catalogue_instructions_are_never_promoted(mini, build):
    for i in range(6):
        mini.viewer(f"U-I{i}", signup=at(days=-1))
    svc = build(mini)
    assert svc.cat.get("T0118").instruction_like is True
    for i in range(6):
        link = "ott://c/cmp1/d1/title/T0118" if i == 0 else None  # even a campaign link to it does not promote it
        svc.handle_trigger(trigger(f"U-I{i}", at(seconds=i), deeplink=link))
    promoted = {t for a in svc.store.all_actions() for t in ([a.get("hero_title_id")] + [x for r in a.get("rows", []) for x in r["title_ids"]] + [a.get("title_id")])}
    assert "T0118" not in promoted
    assert not [a for a in svc.store.all_actions() if a["type"] == "whatsapp"]  # no "message everyone" blast


def test_unsupported_model_eligibility_claim_is_rejected(mini, build):
    mini.viewer("U-CLAIM", status="never", signup=at(days=-1))
    t = trigger("U-CLAIM", at())
    mini.faults.append({"trigger_event_id": t["event_id"], "user_id": "U-CLAIM", "fault": "unsupported_claim",
                        "model_output": {"goal_id": "G04", "eligible": True, "confidence": 0.97,
                                         "reason": "The user is on an active trial that ends tomorrow (confirmed in billing)."}})
    svc = build(mini)
    d = svc.handle_trigger(t)["decision"]
    assert d["model"]["outcome"] == "rejected_unsupported"
    assert d["goal"]["id"] != "G04"
    assert "not in ['trial_active']" in d["model"]["detail"]
    assert d["status"] == "PUBLISHED"
    assert not any("billing" in e["id"] for e in d["evidence_details"])  # model text never becomes evidence


def test_unmeasurable_goals_are_never_committed(mini, build):
    mini.viewer("U-ANY", status="active")
    svc = build(mini)
    d = svc.handle_trigger(trigger("U-ANY", at()))["decision"]
    ev = {e["goal_id"]: e for e in d["goal_evaluations"]}
    assert not ev["G10"]["eligible"] and "unmeasurable" in ev["G10"]["reason"]
    assert not ev["G11"]["eligible"] and "tv_app_open" in ev["G11"]["reason"]
    assert d["goal"]["id"] not in ("G10", "G11")


def test_unknown_app_dialect_means_no_action(mini, build):
    mini.viewer("U-D7", signup=at(days=-1))
    svc = build(mini)
    d = svc.handle_trigger(trigger("U-D7", at(), dialect="d7"))["decision"]
    assert d["status"] == "NO_ACTION" and d["no_action_kind"] == "no_dialect"
    assert d["goal"] is not None  # the goal still fits the viewer; only the action is impossible


def test_resume_goal_schedules_one_compliant_follow_up(mini, build):
    mini.viewer("U-SER", status="active", push=True)
    for ep in (1, 2, 3):
        mini.event("U-SER", "play_started", at(days=-5, minutes=ep), {"title_id": "T0106", "episode_id": f"show-6-d1-s1e{ep}"})
    svc = build(mini)
    d = svc.handle_trigger(trigger("U-SER", at()))["decision"]
    assert d["goal"]["id"] == "G07" and d["goal"]["params"]["content_slug"] == "show-6-d1"
    push = [a for a in svc.store.all_actions() if a["type"] == "push"]
    assert len(push) == 1
    assert push[0]["send_at"] == "2026-10-02T13:30:00.000Z"  # 19:00 IST the next day: outside quiet hours
    assert push[0]["title_id"] == "T0106" and push[0]["deeplink"] == "ott://title/T0106"
    page = next(a for a in svc.store.all_actions() if a["type"] == "home_page")
    assert page["hero_title_id"] == "T0106"
    # A later open (page expired) for the same plan does not add a second message.
    svc.handle_trigger(trigger("U-SER", at(hours=3)))
    assert len([a for a in svc.store.all_actions() if a["type"] == "push"]) == 1


# -- verifier unit checks: the gate can disagree with the planner ------------------------------------------------

@pytest.fixture
def gate(mini, build):
    mini.viewer("U-V", status="active", push=True, whatsapp=False)
    svc = build(mini)
    rows = svc.store.profile_rows("U-V")
    rule = svc.rules.current("G08")

    def run(page=None, message=None, message_days=frozenset(), trig_dialect="d1", events=()):
        return svc.verifier.verify(
            viewer_id="U-V", trigger=trigger("U-V", at(), dialect=trig_dialect), profile_rows=rows, events=list(events),
            goal_id="G08", rule=rule, evidence=[], page=page, message=message, publish_at=T0,
            deadline=T0 + timedelta(days=7), message_days=set(message_days), tool_calls=5, model_calls=1, latency_s=0.5)
    return run


def _push(**kw):
    base = dict(channel="push", send_at=ist_at((T0 + timedelta(days=1)).date(), 19), title_id="T0101",
                text={"title": "Title T0101", "body": "Title T0101 aapki boli mein. Abhi dekhiye."},
                deeplink="ott://title/T0101")
    base.update(kw)
    return MessageProposal(**base)


def test_verifier_passes_a_valid_page_and_push(gate):
    v = gate(page=PageProposal("d1", "T0111", [{"row_type": "popular_in_dialect", "title_ids": ["T0101", "T0102", "T0103"]}]),
             message=_push())
    assert not v.failed, v.failed


def test_verifier_rejects_wrong_dialect_and_duplicate_content(gate):
    v = gate(page=PageProposal("d1", None, [{"row_type": "popular_in_dialect", "title_ids": ["T0101", "T0201", "T0117"]}]))
    assert not v.page_ok
    assert "T0201" in v.bad_titles  # d2 title on a d1 page
    assert any(f.startswith("page.unique_content") for f in v.failed)  # T0117 is a language variant of T0101
    assert not gate(page=PageProposal("d2", None, [{"row_type": "popular_in_dialect", "title_ids": ["T0201", "T0202", "T0203"]}])).page_ok


@pytest.mark.parametrize("message,needle", [
    (_push(send_at=ist_at(T0.date(), 23)), "message.quiet_hours"),
    (_push(text={"title": "Title T0101", "body": "Hurry, last chance to watch Title T0101"}), "message.forbidden_phrases"),
    (_push(text={"title": "Title T0101", "body": "Title T0101 for just ₹99"}), "message.prices_exist"),
    (_push(title_id="T0201", deeplink="ott://title/T0201", text={"title": "Title T0201", "body": "Title T0201"}), "message.title_valid"),
    (_push(deeplink="https://evil.example/T0101"), "message.deeplink"),
    (MessageProposal("whatsapp", ist_at((T0 + timedelta(days=1)).date(), 19), "T0101", template_id="wa_free_text",
                     variables={"title_name": "Title T0101"}), "message.consent"),
])
def test_verifier_blocks_policy_violations(gate, message, needle):
    v = gate(message=message)
    assert not v.message_ok
    assert any(f.startswith(needle) for f in v.failed), v.failed


def test_verifier_enforces_frequency_cap_and_opt_out(gate):
    m = _push()
    assert not gate(message=m, message_days={str((T0 + timedelta(days=1)).date())}).message_ok
    opt_out = {"event_id": "o1", "event": "notification_opt_out", "received_at": at(hours=-1), "properties": {"channel": "push"}}
    assert not gate(message=m, events=[opt_out]).message_ok
    assert gate(message=_push(text={"title": "Title T0101", "body": "Title T0101 for ₹149 a month"})).message_ok


def test_frequency_cap_is_also_a_database_invariant(mini, build):
    mini.viewer("U-CAP")
    svc = build(mini)
    import sqlite3
    base = {"decision_id": "D-X", "user_id": "U-CAP", "published_at": at(), "send_at": at(hours=8)}
    svc.store.conn.execute("INSERT INTO decisions (decision_id, viewer_id, trigger_event_id, deliver_at, status, action, payload) "
                           "VALUES ('D-X','U-CAP','e1',?, 'PUBLISHED','PUSH','{}')", (at(),))
    svc.store.save_action(svc.store.conn, {**base, "action_id": "A-X1", "type": "push"}, "2026-10-02")
    with pytest.raises(sqlite3.IntegrityError):
        svc.store.save_action(svc.store.conn, {**base, "action_id": "A-X2", "type": "whatsapp", "decision_id": "D-X"}, "2026-10-02")
