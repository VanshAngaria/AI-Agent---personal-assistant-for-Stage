"""Understand the viewer: turn raw profile rows and events into facts with provenance, freshness and conflicts.

Precedence is defined per fact (see ARCHITECTURE.md, "Source precedence"). No fact is decided by
"newest row wins" or "largest source wins" by default, and the language model is never a source.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from oua.catalogue import CATALOGUE_DIALECTS, Catalogue
from oua.timeutil import ist_date, iso, parse_ts, seconds

DEEPLINK = re.compile(r"^ott://c/(?P<campaign>[^/]+)/(?P<dialect>[^/]+)/title/(?P<title_id>[^/?#]+)")
STATUS_EVENTS = ("purchase_completed", "subscription_renewed", "subscription_lapsed")


@dataclass
class Fact:
    key: str
    value: Any
    source: str
    source_id: str | None
    observed_at: str | None
    freshness_s: float | None
    note: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {k: v for k, v in self.__dict__.items() if v is not None or k in ("value",)}

    @property
    def ref(self) -> str:
        return f"{self.source}:{self.source_id}" if self.source_id else self.source


@dataclass
class Conflict:
    fact: str
    values: list[dict[str, Any]]
    chosen: str | None
    why: str


@dataclass
class TriggerTiming:
    received_at: datetime
    deliver_at: datetime
    occurred_at: datetime | None
    sent_at: datetime | None
    event_age_s: float | None          # received - occurred (raw; device clock can distort it)
    buffered_s: float | None           # sent - occurred (both device clock, so clock offset cancels)
    clock_skew_suspected: bool
    live_visit: bool
    reason: str


@dataclass
class SeriesProgress:
    content_slug: str
    title_id: str
    content_type: str
    furthest_index: int
    total_episodes: int
    last_play_at: datetime
    last_play_event_id: str

    @property
    def unfinished(self) -> bool:
        return self.content_type in ("show", "micro_drama") and self.total_episodes > 1 and self.furthest_index < self.total_episodes - 1


@dataclass
class ViewerContext:
    viewer_id: str
    known_at: datetime
    trigger: dict[str, Any]
    timing: TriggerTiming
    history_start: datetime
    profile: dict[str, Any] | None
    profile_rows: list[dict[str, Any]]
    events: list[dict[str, Any]]
    facts: dict[str, Fact] = field(default_factory=dict)
    conflicts: list[Conflict] = field(default_factory=list)
    data_quality: list[str] = field(default_factory=list)
    stale_snapshot: bool = False

    # Derived views used by goals/planner (each also has a Fact in `facts`).
    dialect: str | None = None
    language: str | None = None
    status: str | None = None
    ever_paid: bool = False
    signup_at: datetime | None = None
    consent_push: bool = False
    consent_whatsapp: bool = False
    plays: list[dict[str, Any]] = field(default_factory=list)
    completions: list[dict[str, Any]] = field(default_factory=list)
    app_opens: list[dict[str, Any]] = field(default_factory=list)
    lapse_events: list[dict[str, Any]] = field(default_factory=list)
    series: list[SeriesProgress] = field(default_factory=list)
    top_genres: list[str] = field(default_factory=list)
    watched_slugs: set[str] = field(default_factory=set)
    completed_slugs: set[str] = field(default_factory=set)
    deeplink: dict[str, str] | None = None

    def fact(self, key: str) -> Fact | None:
        return self.facts.get(key)

    def add(self, f: Fact) -> Fact:
        self.facts[f.key] = f
        return f

    def facts_list(self) -> list[dict[str, Any]]:
        return [f.as_dict() for f in self.facts.values()]

    def conflicts_list(self) -> list[dict[str, Any]]:
        return [c.__dict__ for c in self.conflicts]


def trigger_timing(trigger: dict[str, Any], deliver_at: datetime, stale_after_s: float) -> TriggerTiming:
    received = parse_ts(trigger["received_at"])
    occurred = parse_ts(trigger.get("occurred_at"))
    sent = parse_ts(trigger.get("sent_at"))
    age = seconds(occurred, received) if occurred else None
    buffered = seconds(occurred, sent) if occurred and sent else None
    skew = bool(sent and abs(seconds(sent, received)) > 3600) or bool(occurred and occurred > received)
    if buffered is not None and buffered > stale_after_s:
        live, reason = False, (
            f"buffered offline for {buffered:.0f}s on the device (sent_at - occurred_at) before upload; "
            "the viewer is not in the app now"
        )
    elif buffered is None and age is not None and age > stale_after_s and not skew:
        live, reason = False, f"event is {age:.0f}s old on arrival"
    else:
        live, reason = True, "fresh app open; viewer is in the app now"
    return TriggerTiming(received, deliver_at, occurred, sent, age, buffered, skew, live, reason)


def _freshness(known_at: datetime, observed: datetime | None) -> float | None:
    return None if observed is None else round(seconds(observed, known_at), 1)


def reconcile(
    *,
    trigger: dict[str, Any],
    known_at: datetime,
    profile_rows: list[dict[str, Any]],
    events: list[dict[str, Any]],
    catalogue: Catalogue,
    history_start: datetime,
    stale_after_s: float,
    stale_snapshot: bool = False,
    snapshot_at: str | None = None,
) -> ViewerContext:
    viewer = trigger["user_id"]
    ctx = ViewerContext(
        viewer_id=viewer,
        known_at=known_at,
        trigger=trigger,
        timing=trigger_timing(trigger, known_at, stale_after_s),
        history_start=history_start,
        profile=None,
        profile_rows=profile_rows,
        events=events,
        stale_snapshot=stale_snapshot,
    )
    if stale_snapshot:
        ctx.data_quality.append("user data store unavailable; using the last snapshot this agent cached for the viewer")
    _profile(ctx, snapshot_at)
    _dialect(ctx, catalogue)
    _status(ctx)
    _signup(ctx)
    _consent(ctx)
    _history(ctx, catalogue)
    return ctx


# -- profile ---------------------------------------------------------------------------------------------------

def _profile(ctx: ViewerContext, snapshot_at: str | None = None) -> None:
    rows = ctx.profile_rows
    if not rows:
        ctx.data_quality.append("no warehouse profile row (new viewer or missing export); profile facts unknown")
        ctx.add(Fact("profile.present", False, "profile_snapshot", f"export@{snapshot_at}" if snapshot_at else None,
                     snapshot_at, _freshness(ctx.known_at, parse_ts(snapshot_at)),
                     "absent from this warehouse export; an absent row is not evidence of status 'never'"))
        return
    ordered = sorted(rows, key=lambda r: r.get("updated_at") or "")
    working = ordered[-1]
    ctx.profile = working
    upd = parse_ts(working.get("updated_at"))
    ctx.add(Fact("profile.present", True, "profile_snapshot", f"{ctx.viewer_id}@{working.get('updated_at')}",
                 working.get("updated_at"), _freshness(ctx.known_at, upd),
                 f"snapshot_at {working.get('snapshot_at')}; values may be a day stale"))
    if len(rows) > 1:
        tie = len({r.get("updated_at") for r in rows}) < len(rows)
        diffs = sorted({k for r in rows for k in r if not k.startswith("_") and r.get(k) != working.get(k)})
        ctx.conflicts.append(Conflict(
            fact="profile_row",
            values=[{"source": "profile_snapshot", "updated_at": r.get("updated_at"),
                     "subscription_status": r.get("subscription_status"),
                     "preferred_dialect": r.get("preferred_dialect")} for r in ordered],
            chosen=None if tie else f"profile_snapshot@{working.get('updated_at')}",
            why=("two rows share updated_at; neither can be preferred, so status-gated goals are refused"
                 if tie else
                 f"warehouse holds {len(rows)} versions differing in {', '.join(diffs) or 'nothing'}; "
                 "the later updated_at is the working row, the other stays visible"),
        ))
        if tie:
            ctx.profile = None
            ctx.data_quality.append("duplicate profile rows with identical updated_at")


# -- dialect ---------------------------------------------------------------------------------------------------

def _dialect(ctx: ViewerContext, catalogue: Catalogue) -> None:
    t = ctx.trigger
    app = t.get("app_dialect")
    ctx.language = t.get("app_language")
    received = parse_ts(t["received_at"])
    values = []
    if app in CATALOGUE_DIALECTS:
        ctx.dialect = app
        ctx.add(Fact("dialect.app_setting", app, "event", t["event_id"], t["received_at"],
                     _freshness(ctx.known_at, received), "app settings feed on the trigger; the app requests this dialect's page"))
    else:
        ctx.add(Fact("dialect.app_setting", app, "event", t["event_id"], t["received_at"],
                     _freshness(ctx.known_at, received),
                     "not a catalogue dialect (d1-d6); the app cannot show a personalised page for it"))
    values.append({"source": "app_setting", "value": app, "ref": f"event:{t['event_id']}"})

    prev = next((e for e in reversed(ctx.events)
                 if e["event_id"] != t["event_id"] and e.get("app_dialect") in CATALOGUE_DIALECTS), None)
    if prev and prev["app_dialect"] != app:
        values.append({"source": "app_setting_previous", "value": prev["app_dialect"], "ref": f"event:{prev['event_id']}"})
    if ctx.profile and ctx.profile.get("preferred_dialect"):
        pd = ctx.profile["preferred_dialect"]
        ctx.add(Fact("dialect.profile_preferred", pd, "profile_snapshot",
                     f"{ctx.viewer_id}@{ctx.profile.get('updated_at')}", ctx.profile.get("updated_at"),
                     _freshness(ctx.known_at, parse_ts(ctx.profile.get("updated_at")))))
        if pd != app:
            values.append({"source": "profile", "value": pd, "ref": f"profile:{ctx.viewer_id}"})
    link = (t.get("properties") or {}).get("deeplink")
    if link:
        m = DEEPLINK.match(link)
        if m:
            ctx.deeplink = m.groupdict()
            ctx.add(Fact("dialect.campaign_link", m["dialect"], "event", t["event_id"], t["received_at"],
                         _freshness(ctx.known_at, received), f"campaign {m['campaign']} link to {m['title_id']}"))
            if m["dialect"] != app:
                values.append({"source": "campaign_link", "value": m["dialect"], "ref": f"event:{t['event_id']}"})
    if len(values) > 1:
        ctx.conflicts.append(Conflict(
            fact="dialect",
            values=values,
            chosen=f"app_setting:{app}" if ctx.dialect else None,
            why=("the app requests the home page for the dialect it is currently set to, so content must be in the "
                 "app-setting dialect; profile and campaign link describe preference/acquisition, not what is shown"
                 if ctx.dialect else
                 f"app is set to {app!r}, which has no catalogue content; profile/link dialects would not be shown, "
                 "so no dialect can be used"),
        ))


# -- subscription status -------------------------------------------------------------------------------------

def _status_from_event(e: dict[str, Any]) -> str:
    if e["event"] == "subscription_lapsed":
        return "lapsed"
    if e["event"] == "purchase_completed" and (e["properties"] or {}).get("is_trial") is True:
        return "trial_active"
    return "active"


def _status(ctx: ViewerContext) -> None:
    status_events = [e for e in ctx.events if e["event"] in STATUS_EVENTS]
    paid_events = [e for e in status_events if e["event"] == "subscription_renewed"
                   or (e["event"] == "purchase_completed" and (e["properties"] or {}).get("is_trial") is not True)]
    ctx.lapse_events = [e for e in status_events if e["event"] == "subscription_lapsed"]
    prof_status = ctx.profile.get("subscription_status") if ctx.profile else None
    prof_upd = ctx.profile.get("updated_at") if ctx.profile else None
    newer = [e for e in status_events if prof_upd is None or e["received_at"] > prof_upd]

    if newer:
        last = newer[-1]
        ctx.status = _status_from_event(last)
        ctx.add(Fact("subscription_status", ctx.status, "event", last["event_id"], last["received_at"],
                     _freshness(ctx.known_at, parse_ts(last["received_at"])),
                     f"{last['event']} received after the profile row" if prof_upd else f"{last['event']}; no profile row"))
        if prof_status and prof_status != ctx.status:
            ctx.conflicts.append(Conflict(
                fact="subscription_status",
                values=[{"source": "profile", "value": prof_status, "at": prof_upd},
                        {"source": "event", "value": ctx.status, "at": last["received_at"], "ref": f"event:{last['event_id']}"}],
                chosen=f"event:{last['event_id']}",
                why="a billing event received after the warehouse row was exported is newer evidence of the same fact",
            ))
    elif prof_status:
        ctx.status = prof_status
        ctx.add(Fact("subscription_status", prof_status, "profile_snapshot", f"{ctx.viewer_id}@{prof_upd}", prof_upd,
                     _freshness(ctx.known_at, parse_ts(prof_upd)), "no billing event received since the export"))
    else:
        ctx.status = None
        ctx.add(Fact("subscription_status", None, "none", None, None, None,
                     "unknown: no profile row and no billing events; status-gated goals are not eligible"))

    ctx.ever_paid = bool(paid_events) or prof_status in ("active", "lapsed") or ctx.status in ("active", "lapsed")
    src = paid_events[-1] if paid_events else None
    ctx.add(Fact("ever_paid", ctx.ever_paid, "event" if src else "profile_snapshot",
                 src["event_id"] if src else (f"{ctx.viewer_id}@{prof_upd}" if prof_upd else None),
                 src["received_at"] if src else prof_upd,
                 _freshness(ctx.known_at, parse_ts(src["received_at"] if src else prof_upd))))
    if prof_status == "never" and paid_events:
        ctx.conflicts.append(Conflict(
            fact="ever_paid",
            values=[{"source": "profile", "value": "never", "at": prof_upd},
                    {"source": "event", "value": "paid", "ref": f"event:{paid_events[-1]['event_id']}"}],
            chosen=f"event:{paid_events[-1]['event_id']}",
            why="an observed paid purchase cannot be undone by a profile column that says 'never'",
        ))


# -- signup ----------------------------------------------------------------------------------------------------

def _signup(ctx: ViewerContext) -> None:
    ev = next((e for e in ctx.events if e["event"] == "signup_completed"), None)
    prof = parse_ts(ctx.profile.get("signup_at")) if ctx.profile else None
    if ev:
        ctx.signup_at = parse_ts(ev["received_at"])
        ctx.add(Fact("signup_at", ev["received_at"], "event", ev["event_id"], ev["received_at"],
                     _freshness(ctx.known_at, ctx.signup_at), "observed signup_completed (server clock)"))
        if prof and abs(seconds(prof, ctx.signup_at)) > 86400:
            ctx.conflicts.append(Conflict(
                fact="signup_at",
                values=[{"source": "event", "value": ev["received_at"]}, {"source": "profile", "value": iso(prof)}],
                chosen=f"event:{ev['event_id']}",
                why="the observed signup event is primary evidence; the profile column differs by more than a day",
            ))
    elif prof:
        ctx.signup_at = prof
        ctx.add(Fact("signup_at", iso(prof), "profile_snapshot", f"{ctx.viewer_id}@{ctx.profile.get('updated_at')}",
                     ctx.profile.get("updated_at"), _freshness(ctx.known_at, parse_ts(ctx.profile.get("updated_at")))))
    else:
        ctx.add(Fact("signup_at", None, "none", None, None, None, "unknown"))


# -- consent ---------------------------------------------------------------------------------------------------

def _consent(ctx: ViewerContext) -> None:
    opt_outs = {(e["properties"] or {}).get("channel"): e for e in ctx.events if e["event"] == "notification_opt_out"}
    for channel, field_name, attr in (("push", "notifications_opt_in", "consent_push"),
                                      ("whatsapp", "whatsapp_opt_in", "consent_whatsapp")):
        snap = ctx.profile.get(field_name) if ctx.profile else None
        out = opt_outs.get(channel)
        if out:
            value, f = False, Fact(f"consent.{channel}", False, "event", out["event_id"], out["received_at"],
                                   _freshness(ctx.known_at, parse_ts(out["received_at"])),
                                   "notification_opt_out overrides the snapshot (policy rule 1)")
            if snap == "true":
                ctx.conflicts.append(Conflict(
                    fact=f"consent.{channel}",
                    values=[{"source": "profile", "value": True}, {"source": "event", "value": False, "ref": f"event:{out['event_id']}"}],
                    chosen=f"event:{out['event_id']}", why="an opt-out always overrides the snapshot opt-in"))
        elif snap is None:
            value, f = False, Fact(f"consent.{channel}", False, "none", None, None, None, "no profile row: consent unknown, treated as not given")
        else:
            value = snap == "true"
            f = Fact(f"consent.{channel}", value, "profile_snapshot", f"{ctx.viewer_id}@{ctx.profile.get('updated_at')}",
                     ctx.profile.get("updated_at"), _freshness(ctx.known_at, parse_ts(ctx.profile.get("updated_at"))))
        setattr(ctx, attr, value)
        ctx.add(f)


# -- behavioural history ---------------------------------------------------------------------------------------

def _history(ctx: ViewerContext, catalogue: Catalogue) -> None:
    trig = ctx.trigger
    for e in ctx.events:
        if e["event"] == "play_started":
            ctx.plays.append(e)
        elif e["event"] == "episode_completed":
            ctx.completions.append(e)
        elif e["event"] == "app_open" and e["event_id"] != trig["event_id"] and e.get("session_id") != trig.get("session_id"):
            ctx.app_opens.append(e)

    genres: Counter = Counter()
    progress: dict[str, SeriesProgress] = {}
    for e in ctx.plays + ctx.completions:
        props = e["properties"] or {}
        t = catalogue.get(props.get("title_id"))
        if not t:
            continue
        ctx.watched_slugs.add(t.content_slug)
        if e["event"] == "play_started":
            for g in t.genres:
                genres[g] += 1
        else:
            ctx.completed_slugs.add(t.content_slug)
        idx = catalogue.episode_index(t.content_slug).get(props.get("episode_id"), 0)
        at = parse_ts(e["received_at"])
        sp = progress.get(t.content_slug)
        if sp is None:
            progress[t.content_slug] = SeriesProgress(t.content_slug, t.title_id, t.content_type, idx,
                                                      catalogue.episode_count(t.content_slug), at, e["event_id"])
        else:
            sp.furthest_index = max(sp.furthest_index, idx)
            if e["event"] == "play_started" and at >= sp.last_play_at:
                sp.last_play_at, sp.last_play_event_id, sp.title_id = at, e["event_id"], t.title_id
    ctx.series = list(progress.values())
    ctx.top_genres = [g for g, _ in genres.most_common(3)]

    cutoff14 = ctx.known_at - timedelta(days=14)
    play_days = sorted({str(ist_date(parse_ts(e["received_at"]))) for e in ctx.plays if parse_ts(e["received_at"]) >= cutoff14})
    opens14 = [e for e in ctx.app_opens if parse_ts(e["received_at"]) >= cutoff14]
    ctx.add(Fact("history.window", {"from": iso(ctx.history_start), "to": iso(ctx.known_at)}, "event_stream", None,
                  iso(ctx.history_start), None,
                  "only ~30 days of events exist; 'never did X' is provable only for viewers who signed up inside it"))
    ctx.add(Fact("history.play_started_count", len(ctx.plays), "event_stream",
                 ctx.plays[-1]["event_id"] if ctx.plays else None,
                 ctx.plays[-1]["received_at"] if ctx.plays else None,
                 _freshness(ctx.known_at, parse_ts(ctx.plays[-1]["received_at"])) if ctx.plays else None))
    ctx.add(Fact("history.episode_completed_count", len(ctx.completions), "event_stream",
                 ctx.completions[-1]["event_id"] if ctx.completions else None,
                 ctx.completions[-1]["received_at"] if ctx.completions else None,
                 _freshness(ctx.known_at, parse_ts(ctx.completions[-1]["received_at"])) if ctx.completions else None))
    ctx.add(Fact("history.play_days_14d", play_days, "event_stream", None, None, None))
    ctx.add(Fact("history.app_opens_14d_before_trigger", len(opens14), "event_stream",
                 opens14[-1]["event_id"] if opens14 else None, opens14[-1]["received_at"] if opens14 else None,
                 _freshness(ctx.known_at, parse_ts(opens14[-1]["received_at"])) if opens14 else None,
                 "excludes the current trigger and its session"))
    ctx.add(Fact("trigger.live_visit", ctx.timing.live_visit, "event", trig["event_id"], trig["received_at"],
                 _freshness(ctx.known_at, parse_ts(trig["received_at"])), ctx.timing.reason))
