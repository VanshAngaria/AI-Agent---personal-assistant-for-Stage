"""Brief section 8: conflicting sources are resolved by per-fact precedence and stay visible."""

from oua.reconcile import reconcile
from tests.conftest import T0, at, trigger


def test_three_dialects_app_setting_wins_and_others_stay_visible(mini, build):
    mini.viewer("U-TRI", signup=at(days=-1), dialect="d2")  # profile says d2
    svc = build(mini)
    t = trigger("U-TRI", at(), dialect="d1", deeplink="ott://c/cmp9/d3/title/T0201")  # app d1, campaign link d3
    d = svc.handle_trigger(t)["decision"]
    assert d["dialect"]["used"] == "d1" and d["dialect"]["source"] == "app_setting"
    assert set(d["dialect"]["conflicts"]) >= {"profile:d2", "campaign_link:d3"}
    conflict = next(c for c in d["conflicting_evidence"] if c["fact"] == "dialect")
    assert conflict["chosen"] == "app_setting:d1" and "currently set to" in conflict["why"]
    assert d["status"] == "PUBLISHED"
    for ref in d["content"]:
        assert svc.cat.get(ref.split(":")[1]).dialect == "d1"
    assert "title:T0201" not in d["content"]  # the linked title is in another dialect, so it is not promoted


def test_duplicate_profile_rows_later_row_wins_and_conflict_is_recorded(mini, build):
    mini.viewer("U-DUP", status="active", updated=at(days=-5))
    mini.viewer("U-DUP", status="never", updated=at(hours=-20))
    svc = build(mini)
    d = svc.handle_trigger(trigger("U-DUP", at()))["decision"]
    status = next(e for e in d["evidence_details"] if e["id"] == "fact:subscription_status")
    assert status["value"] == "never"
    row_conflict = next(c for c in d["conflicting_evidence"] if c["fact"] == "profile_row")
    assert len(row_conflict["values"]) == 2 and row_conflict["chosen"].endswith(at(hours=-20))


def test_purchase_event_after_profile_export_overrides_never(mini, build):
    mini.viewer("U-PAID", status="never", updated=at(hours=-30))
    mini.event("U-PAID", "purchase_completed", at(hours=-2), {"plan_id": "P_MONTH", "price_inr": 149, "is_trial": False})
    svc = build(mini)
    d = svc.handle_trigger(trigger("U-PAID", at()))["decision"]
    status = next(e for e in d["evidence_details"] if e["id"] == "fact:subscription_status")
    assert status["value"] == "active" and status["source"] == "event"
    assert any(c["fact"] == "subscription_status" for c in d["conflicting_evidence"])
    g05 = next(e for e in d["goal_evaluations"] if e["goal_id"] == "G05")
    assert g05["eligible"] is False


def test_opt_out_event_overrides_snapshot_consent(mini, build):
    mini.viewer("U-OUT", push=True)
    mini.event("U-OUT", "notification_opt_out", at(days=-3), {"channel": "push"})
    svc = build(mini)
    rows = svc.store.profile_rows("U-OUT")
    events = svc.store.events_for("U-OUT", at())
    ctx = reconcile(trigger=trigger("U-OUT", at()), known_at=T0, profile_rows=rows, events=events, catalogue=svc.cat,
                    history_start=svc.history_start, stale_after_s=600)
    assert ctx.consent_push is False
    assert ctx.fact("consent.push").source == "event"


def test_missing_profile_is_unknown_not_never(mini, build):
    mini.viewer("U-NOPROF", profile=False)
    mini.event("U-NOPROF", "app_open", at(days=-3))
    svc = build(mini)
    d = svc.handle_trigger(trigger("U-NOPROF", at()))["decision"]
    ev = {e["goal_id"]: e for e in d["goal_evaluations"]}
    assert ev["G05"]["eligible"] is False and "unknown" in ev["G05"]["reason"]
    assert any("no warehouse profile row" in q for q in d["data_quality"])
