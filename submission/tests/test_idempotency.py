"""Brief section 8: late or duplicate triggers are handled idempotently."""

from tests.conftest import at, trigger


def _new_viewer(mini, uid="U-NEW", **kw):
    mini.viewer(uid, signup=at(days=-1), **kw)


def test_duplicate_trigger_returns_the_stored_decision_and_acts_once(mini, build):
    _new_viewer(mini)
    svc = build(mini)
    t = trigger("U-NEW", at())
    first = svc.handle_trigger(t)
    again = dict(t, received_at=at(seconds=40), deliver_at=at(seconds=40))  # same event_id, later delivery
    second = svc.handle_trigger(again)

    assert first["decision"]["status"] == "PUBLISHED"
    assert second["redelivery"] is True
    assert second["decision_id"] == first["decision"]["decision_id"]
    assert len(svc.store.all_decisions()) == 1
    assert len(svc.store.all_actions()) == len(first["actions"])
    row = svc.store.conn.execute("SELECT deliveries FROM processed_triggers WHERE event_id = ?", (t["event_id"],)).fetchone()
    assert row["deliveries"] == 2


def test_trigger_claimed_but_never_decided_is_decided_on_redelivery(mini, build):
    _new_viewer(mini)
    svc = build(mini)
    t = trigger("U-NEW", at())
    svc.store.claim_trigger(t["event_id"], "U-NEW", at())  # simulate a crash after the claim, before the commit
    res = svc.handle_trigger(dict(t, received_at=at(seconds=30), deliver_at=at(seconds=30)))
    assert res["redelivery"] is False and res["decision"]["status"] == "PUBLISHED"
    assert len(svc.store.all_decisions()) == 1


def test_buffered_offline_open_is_history_not_a_visit(mini, build):
    _new_viewer(mini)
    svc = build(mini)
    late = trigger("U-NEW", at(), buffered_s=2 * 86400)  # opened two days ago, uploaded now
    res = svc.handle_trigger(late)
    d = res["decision"]
    assert d["status"] == "NO_ACTION" and d["no_action_kind"] == "stale_trigger"
    assert d["trigger"]["live_visit"] is False and d["trigger"]["buffered_s"] > 86000
    assert res["actions"] == []
    assert svc.store.open_plan("U-NEW") is None  # a stale open does not start or change a plan
    assert svc.store.get_event(late["event_id"]) is not None  # but it is kept as history


def test_device_clock_offset_alone_does_not_make_a_trigger_stale(mini, build):
    _new_viewer(mini)
    svc = build(mini)
    t = trigger("U-NEW", at())
    t["occurred_at"], t["sent_at"] = at(hours=-5, seconds=-2), at(hours=-5, seconds=-1)  # phone clock 5h behind
    d = svc.handle_trigger(t)["decision"]
    assert d["trigger"]["live_visit"] is True
    assert d["trigger"]["clock_skew_suspected"] is True
    assert d["status"] == "PUBLISHED"


def test_history_redelivery_keeps_one_event_with_earliest_receipt(mini, build):
    _new_viewer(mini)
    e = mini.event("U-NEW", "play_started", at(hours=-3), {"title_id": "T0101", "episode_id": "micro-1-d1-s1e1"})
    mini.events.append(dict(e, received_at=at(hours=-1)))  # redelivered later with the same event_id
    svc = build(mini)
    rows = svc.store.conn.execute("SELECT received_at, deliveries FROM events WHERE event_id = ?", (e["event_id"],)).fetchall()
    assert len(rows) == 1
    assert rows[0]["received_at"] == at(hours=-3) and rows[0]["deliveries"] == 2


def test_redelivered_live_event_is_processed_once(mini, build):
    mini.viewer("U-L", signup=at(days=-1))
    svc = build(mini)
    svc.handle_trigger(trigger("U-L", at()))  # commits G01: first play within 24 h
    play = {**trigger("U-L", at(minutes=3), name="play_started"), "properties": {"title_id": "T0101", "episode_id": "micro-1-d1-s1e1"}}
    first = svc.on_live_event(play)
    second = svc.on_live_event(dict(play, received_at=at(minutes=9), deliver_at=at(minutes=9)))
    assert first["duplicate"] is False and first["plan_closed"] == "succeeded"
    assert second["duplicate"] is True and "plan_closed" not in second
    row = svc.store.conn.execute("SELECT received_at, deliveries FROM events WHERE event_id = ?", (play["event_id"],)).fetchone()
    assert row["deliveries"] == 2 and row["received_at"] == at(minutes=3)


def test_second_open_in_same_session_does_not_duplicate_the_page(mini, build):
    _new_viewer(mini)
    svc = build(mini)
    svc.handle_trigger(trigger("U-NEW", at(), session="s1"))
    d = svc.handle_trigger(trigger("U-NEW", at(minutes=5), session="s1", name="login_success"))["decision"]
    assert d["status"] == "NO_ACTION" and d["no_action_kind"] == "page_already_live"
    assert len([a for a in svc.store.all_actions() if a["type"] == "home_page"]) == 1
