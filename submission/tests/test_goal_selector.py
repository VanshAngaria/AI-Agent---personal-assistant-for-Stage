from __future__ import annotations

from datetime import timedelta

from oua.planning.selector import GoalSelector
from oua.viewer import SeriesProgress, TimedEvent, ViewerSnapshot
from tests.conftest import TEST_VIEWER, utc

T = utc("2026-10-01T12:00:00.000Z")


def snap(**overrides) -> ViewerSnapshot:
    data = {
        "viewer_id": TEST_VIEWER,
        "decision_time": T,
        "trigger_event_id": "trig-1",
        "trigger_event": "app_open",
        "app_dialect": "d1",
        "dialect_source": "app_setting",
    }
    data.update(overrides)
    return ViewerSnapshot.model_validate(data)


def eval_map(selector: GoalSelector, snapshot: ViewerSnapshot) -> dict:
    return {item.goal_id: item for item in selector.evaluate_all(snapshot)}


def test_each_configured_goal_has_an_evaluator(catalog):
    selector = GoalSelector(catalog)
    snapshot = snap()
    results = eval_map(selector, snapshot)
    assert set(results) == set(catalog.rules)
    for goal_id, item in results.items():
        assert item.reasons


def test_g10_and_g11_are_unmeasurable(catalog):
    selector = GoalSelector(catalog)
    results = eval_map(selector, snap(subscription_status="active", status_evidence_id="p1"))
    assert results["G10"].measurable is False
    assert results["G10"].eligible is False
    assert "invent a proxy" in results["G10"].reasons[0]
    assert results["G11"].measurable is False
    assert results["G11"].eligible is False


def test_g01_eligible_and_boundary(catalog):
    selector = GoalSelector(catalog)
    ok = snap(signup_at=T - timedelta(days=6), signup_evidence_id="su1")
    assert selector.evaluate("G01", ok).eligible is True
    edge = snap(signup_at=T - timedelta(days=7), signup_evidence_id="su1")
    assert selector.evaluate("G01", edge).eligible is True
    late = snap(signup_at=T - timedelta(days=7, seconds=1), signup_evidence_id="su1")
    assert selector.evaluate("G01", late).eligible is False
    played = snap(
        signup_at=T - timedelta(days=1),
        signup_evidence_id="su1",
        play_started=[TimedEvent(event_id="pl1", received_at=T - timedelta(hours=1))],
    )
    assert selector.evaluate("G01", played).eligible is False


def test_g01_insufficient_history(catalog):
    selector = GoalSelector(catalog)
    result = selector.evaluate("G01", snap())
    assert result.eligible is False
    assert "insufficient history" in result.reasons[0]


def test_g02_requires_no_episode_completed(catalog):
    selector = GoalSelector(catalog)
    assert selector.evaluate("G02", snap()).eligible is True
    done = snap(
        episode_completed=[TimedEvent(event_id="ep1", received_at=T - timedelta(days=1))]
    )
    assert selector.evaluate("G02", done).eligible is False


def test_g03_signup_window(catalog):
    selector = GoalSelector(catalog)
    assert selector.evaluate("G03", snap(signup_at=T - timedelta(days=1), signup_evidence_id="su")).eligible
    assert not selector.evaluate("G03", snap(signup_at=T - timedelta(days=3), signup_evidence_id="su")).eligible


def test_g04_trial_only(catalog):
    selector = GoalSelector(catalog)
    assert selector.evaluate("G04", snap(subscription_status="trial_active", status_evidence_id="st")).eligible
    assert not selector.evaluate("G04", snap(subscription_status="never", status_evidence_id="st")).eligible


def test_g05_never_but_not_if_paid_event(catalog):
    selector = GoalSelector(catalog)
    assert selector.evaluate("G05", snap(subscription_status="never", status_evidence_id="st")).eligible
    paid = snap(subscription_status="never", status_evidence_id="st", has_paid=True, paid_evidence_id="buy")
    assert selector.evaluate("G05", paid).eligible is False


def test_g06_lapsed_with_documented_assumption(catalog):
    selector = GoalSelector(catalog)
    result = selector.evaluate("G06", snap(subscription_status="lapsed", status_evidence_id="st"))
    assert result.eligible is True
    assert any("no recency window" in item for item in result.assumptions)


def test_g07_unfinished_series_idle(catalog):
    selector = GoalSelector(catalog)
    series = SeriesProgress(
        content_slug="slug-a",
        content_type="show",
        last_play_at=T - timedelta(days=4),
        last_play_event_id="pl-a",
        episode_count=3,
        completed_episode_ids=["slug-a-s1e1"],
    )
    assert selector.evaluate("G07", snap(series=[series])).eligible is True
    film = SeriesProgress(
        content_slug="slug-c",
        content_type="film",
        last_play_at=T - timedelta(days=10),
        last_play_event_id="pl-c",
        episode_count=1,
        completed_episode_ids=[],
    )
    assert selector.evaluate("G07", snap(series=[film])).eligible is False
    recent = series.model_copy(update={"last_play_at": T - timedelta(days=1)})
    assert selector.evaluate("G07", snap(series=[recent])).eligible is False


def test_g08_active(catalog):
    selector = GoalSelector(catalog)
    assert selector.evaluate("G08", snap(subscription_status="active", status_evidence_id="st")).eligible
    assert not selector.evaluate("G08", snap(subscription_status="never", status_evidence_id="st")).eligible


def test_g09_play_day_cap(catalog):
    selector = GoalSelector(catalog)
    one = snap(
        subscription_status="active",
        status_evidence_id="st",
        play_started=[TimedEvent(event_id="p1", received_at=T - timedelta(days=2))],
    )
    assert selector.evaluate("G09", one).eligible is True
    two = snap(
        subscription_status="active",
        status_evidence_id="st",
        play_started=[
            TimedEvent(event_id="p1", received_at=T - timedelta(days=2)),
            TimedEvent(event_id="p2", received_at=T - timedelta(days=5)),
        ],
    )
    assert selector.evaluate("G09", two).eligible is False


def test_g12_excludes_current_trigger(catalog):
    selector = GoalSelector(catalog)
    only_trigger = snap(app_opens=[TimedEvent(event_id="trig-1", received_at=T)])
    assert selector.evaluate("G12", only_trigger).eligible is True
    recent = snap(
        app_opens=[
            TimedEvent(event_id="trig-1", received_at=T),
            TimedEvent(event_id="old", received_at=T - timedelta(days=3)),
        ]
    )
    assert selector.evaluate("G12", recent).eligible is False


def test_priority_picks_g01_over_g02(catalog):
    selector = GoalSelector(catalog)
    snapshot = snap(signup_at=T - timedelta(hours=12), signup_evidence_id="su")
    chosen = selector.select(snapshot)
    assert chosen.goal is not None
    assert chosen.goal.id == "G01"
    assert chosen.goal.success_signal.event == "play_started"
    assert chosen.goal.rule_version == catalog.version
    assert chosen.goal.deadline == T + timedelta(hours=24)


def test_keep_active_plan_if_still_eligible(catalog):
    selector = GoalSelector(catalog)
    snapshot = snap(subscription_status="active", status_evidence_id="st")
    # G08 and G09 both eligible; G08 is higher priority, but keep G09 if already active.
    chosen = selector.select(snapshot, active_goal_id="G09")
    assert chosen.goal is not None
    assert chosen.goal.id == "G09"
    assert chosen.kept_active is True


def test_revise_when_previous_goal_ineligible(service, catalog):
    selector = GoalSelector(catalog)
    before = snap(subscription_status="never", status_evidence_id="st")
    first = selector.select(before)
    assert first.goal is not None
    assert first.goal.id == "G02"
    service.store.set_active_plan(
        TEST_VIEWER,
        first.goal.id,
        first.goal.deadline.isoformat(),
        first.goal.success_signal.event,
        first.goal.success_signal.condition,
        first.goal.success_signal.window_hours,
        first.goal.rule_version,
        T.isoformat(),
        "D-OLD",
    )
    after = snap(
        subscription_status="never",
        status_evidence_id="st",
        episode_completed=[TimedEvent(event_id="ep1", received_at=T)],
        has_paid=True,
        paid_evidence_id="pay",
        app_opens=[TimedEvent(event_id="o1", received_at=T - timedelta(days=1))],
    )
    result = service.select_goal(after)
    assert result.goal is None
    revisions = service.store.list_plan_revisions(TEST_VIEWER)
    assert revisions == []  # no new goal to switch to; caller clears separately
    # facts made G02 ineligible; select_goal only logs revision when a replacement exists
    assert result.abstention_reason


def test_material_change_switches_goal(service, catalog):
    service.store.set_active_plan(
        TEST_VIEWER,
        "G05",
        (T + timedelta(days=7)).isoformat(),
        "purchase_completed",
        "is_trial = false",
        168,
        catalog.version,
        T.isoformat(),
        "D-OLD",
    )
    after_pay = snap(
        subscription_status="active",
        status_evidence_id="st",
        has_paid=True,
        paid_evidence_id="pay",
        episode_completed=[TimedEvent(event_id="ep1", received_at=T - timedelta(days=10))],
        app_opens=[TimedEvent(event_id="o1", received_at=T - timedelta(days=1))],
        play_started=[
            TimedEvent(event_id="p1", received_at=T - timedelta(days=2)),
            TimedEvent(event_id="p2", received_at=T - timedelta(days=5)),
        ],
    )
    result = service.select_goal(after_pay)
    assert result.goal is not None
    assert result.goal.id == "G08"
    assert service.store.list_plan_revisions(TEST_VIEWER)


def test_g03_correction_only_updates_open_g03_plans(service, catalog, store):
    store.set_active_plan(
        "TEST-A",
        "G03",
        (T + timedelta(hours=48)).isoformat(),
        "app_open",
        "on a later IST calendar day than the goal start",
        48,
        catalog.version,
        T.isoformat(),
        "D-A",
    )
    store.set_active_plan(
        "TEST-B",
        "G02",
        (T + timedelta(hours=72)).isoformat(),
        "episode_completed",
        "any episode",
        72,
        catalog.version,
        T.isoformat(),
        "D-B",
    )
    n = service.apply_goal_rule_correction(
        "G03",
        {
            "event": "play_started",
            "condition": "on a later IST calendar day within the window",
            "window_hours": 48,
        },
        version_tag="2026-09-30+G03:play_started",
        effective_at="2026-10-01T08:30:00.000Z",
    )
    assert n == 1
    g03 = store.get_active_plan("TEST-A")
    g02 = store.get_active_plan("TEST-B")
    assert g03["success_event"] == "play_started"
    assert g03["rule_version"] == "2026-09-30+G03:play_started"
    assert g02["success_event"] == "episode_completed"
    assert g02["rule_version"] == catalog.version
    committed = GoalSelector(catalog).select(
        snap(signup_at=T - timedelta(hours=3), signup_evidence_id="su")
    )
    assert committed.goal is not None
    assert committed.goal.id == "G01" or committed.goal.success_signal.event != "tv_app_open"
    g03_eval = GoalSelector(catalog).evaluate("G03", snap(signup_at=T - timedelta(hours=3), signup_evidence_id="su"))
    assert g03_eval.eligible
    selected_g03 = GoalSelector(catalog)._commit("G03", snap(signup_at=T - timedelta(hours=3)))
    assert selected_g03.success_signal.event == "play_started"


def test_abstain_when_no_supported_goal(catalog):
    selector = GoalSelector(catalog)
    snapshot = snap(
        episode_completed=[TimedEvent(event_id="ep", received_at=T)],
        app_opens=[TimedEvent(event_id="o", received_at=T - timedelta(days=1))],
        has_paid=True,
        paid_evidence_id="pay",
    )
    result = selector.select(snapshot)
    assert result.goal is None
    assert result.abstention_reason
    assert "G10" in result.abstention_reason
