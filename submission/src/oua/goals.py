"""Goal rules: versioned rule book, a generic eligibility engine, selection, and success matching.

Nothing here is specific to a goal id. Eligibility = checks derived from the structured fields of config/goals.json
plus the reviewed interpretations in config/agent_policy.json. Success = the goal's success event plus the matchers the
policy names. A new goal that uses existing checks and matchers needs config only. A goal whose eligibility is text-only
and has no reviewed interpretation is refused rather than guessed. A language model may *choose among* eligible goals;
it can never make a goal eligible.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Callable

from oua.catalogue import Catalogue
from oua.policy import Policy
from oua.reconcile import ViewerContext
from oua.timeutil import ist_date, iso, parse_ts

# Events that exist in the pack's event stream or in the simulator's outcome schema. A success signal outside this
# set cannot be observed, so a goal using it cannot be judged and must never be committed.
OBSERVABLE_EVENTS = frozenset({
    "signup_completed", "login_success", "app_open", "app_dialect_changed", "home_viewed", "title_viewed", "search",
    "play_started", "play_progress", "episode_completed", "paywall_viewed", "purchase_completed",
    "subscription_renewed", "subscription_lapsed", "message_delivered", "message_clicked", "notification_opt_out",
    "home_page_shown", "message_failed",
})
STRENGTH_ORDER = {"absence": 0, "snapshot": 1, "observed": 2}


@dataclass
class RuleVersion:
    goal_id: str
    version: str
    name: str
    eligibility: dict[str, Any]
    success: dict[str, Any]
    effective_at: str
    source: str

    @property
    def measurable(self) -> bool:
        ev = self.success.get("event")
        return bool(ev) and ev in OBSERVABLE_EVENTS

    @property
    def window_hours(self) -> int:
        return int(self.success["window_hours"])


class RuleBook:
    """All versions of every goal rule. Corrections add a version; nothing is rewritten in place."""

    def __init__(self, goals_json: dict[str, Any]) -> None:
        self.base_version = goals_json["version"]
        self.versions: dict[str, list[RuleVersion]] = {}
        for g in goals_json["goals"]:
            self.versions[g["goal_id"]] = [RuleVersion(
                goal_id=g["goal_id"], version=f"{g['goal_id']}@{self.base_version}", name=g["name"],
                eligibility=g.get("eligibility") or {}, success=dict(g.get("success") or {}),
                effective_at=f"{self.base_version}T00:00:00.000Z", source="config/goals.json")]

    def current(self, goal_id: str) -> RuleVersion:
        return self.versions[goal_id][-1]

    def get(self, version: str) -> RuleVersion:
        goal_id = version.split("@", 1)[0]
        for v in self.versions[goal_id]:
            if v.version == version:
                return v
        raise KeyError(version)

    def goal_ids(self) -> list[str]:
        return list(self.versions)

    def correct_success(self, goal_id: str, new_success: dict[str, Any], at: str, source: str) -> RuleVersion:
        """Add a corrected version. Fields the correction does not mention (e.g. window_hours) are inherited."""
        prev = self.current(goal_id)
        if not new_success.get("event"):
            raise ValueError(f"correction for {goal_id} names no success event; refusing to make the goal unmeasurable")
        merged = dict(prev.success)
        merged.update({k: v for k, v in new_success.items() if v is not None})
        n = len(self.versions[goal_id]) + 1
        rv = RuleVersion(goal_id, f"{goal_id}@{self.base_version}.r{n}", prev.name, prev.eligibility, merged, at, source)
        self.versions[goal_id].append(rv)
        return rv


@dataclass
class Eligibility:
    goal_id: str
    eligible: bool
    reason: str
    evidence: list[str] = field(default_factory=list)
    assumptions: list[str] = field(default_factory=list)
    params: dict[str, Any] = field(default_factory=dict)
    evidence_strength: str = "observed"  # observed | snapshot | absence
    checks: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {"goal_id": self.goal_id, "eligible": self.eligible, "reason": self.reason, "evidence": self.evidence,
                "checks": self.checks}


@dataclass
class CheckResult:
    ok: bool
    reason: str
    evidence: list[str] = field(default_factory=list)
    strength: str = "observed"
    params: dict[str, Any] = field(default_factory=dict)


def _ev(e: dict[str, Any]) -> str:
    return f"event:{e['event_id']}"


def _fact(ctx: ViewerContext, key: str) -> list[str]:
    return [f"fact:{key}"] if ctx.fact(key) is not None else []


def _fact_strength(ctx: ViewerContext, key: str) -> str:
    f = ctx.fact(key)
    return "observed" if f is not None and f.source == "event" else "snapshot"


def _age_days(ctx: ViewerContext) -> float | None:
    return None if ctx.signup_at is None else (ctx.known_at - ctx.signup_at).total_seconds() / 86400


# == eligibility checks ===========================================================================================
# Each check reads only reconciled facts. Parameters come from goals.json (structured fields) or the policy file.

def chk_signed_up_within_days(ctx: ViewerContext, cat: Catalogue, *, days: float, **_) -> CheckResult:
    age = _age_days(ctx)
    if age is None:
        return CheckResult(False, "signup time unknown")
    if age > days:
        return CheckResult(False, f"signed up {age:.1f}d ago (limit {days}d)", _fact(ctx, "signup_at"))
    return CheckResult(True, f"signed up {age:.1f}d ago (limit {days}d)", _fact(ctx, "signup_at"), _fact_strength(ctx, "signup_at"))


def chk_account_min_age_days(ctx: ViewerContext, cat: Catalogue, *, days: float, **_) -> CheckResult:
    age = _age_days(ctx)
    if age is None:
        return CheckResult(False, "signup unknown: absence cannot be told apart from a brand-new viewer")
    if age < days:
        return CheckResult(False, f"account is {age:.1f}d old; needs {days}d", _fact(ctx, "signup_at"))
    return CheckResult(True, f"account is {age:.0f}d old", _fact(ctx, "signup_at"), _fact_strength(ctx, "signup_at"))


def chk_no_prior_event(ctx: ViewerContext, cat: Catalogue, *, event: str, require_full_history: bool = True, **_) -> CheckResult:
    prior = [e for e in ctx.events if e["event"] == event and e["event_id"] != ctx.trigger["event_id"]]
    if prior:
        return CheckResult(False, f"a prior {event} exists", [_ev(prior[0])])
    if require_full_history and not (ctx.signup_at is not None and ctx.signup_at >= ctx.history_start):
        return CheckResult(False, f"cannot prove 'never {event}': signup predates the event history (or is unknown)",
                           _fact(ctx, "signup_at") + _fact(ctx, "history.window"))
    return CheckResult(True, f"no {event} since signup (history covers the whole account life)",
                       _fact(ctx, "signup_at") + _fact(ctx, "history.window"), "absence")


def chk_subscription_status_in(ctx: ViewerContext, cat: Catalogue, *, values: list[str], **_) -> CheckResult:
    if ctx.status is None:
        return CheckResult(False, "subscription status unknown (no profile row, no billing events)")
    if ctx.status not in values:
        return CheckResult(False, f"status {ctx.status!r} not in {values}", _fact(ctx, "subscription_status"))
    return CheckResult(True, f"status {ctx.status!r}", _fact(ctx, "subscription_status"), _fact_strength(ctx, "subscription_status"))


def chk_never_paid(ctx: ViewerContext, cat: Catalogue, **_) -> CheckResult:
    if ctx.ever_paid:
        return CheckResult(False, "a paid plan is already observed", _fact(ctx, "ever_paid"))
    return CheckResult(True, "no paid purchase or renewal observed", _fact(ctx, "ever_paid"), "snapshot")


def chk_recent_event(ctx: ViewerContext, cat: Catalogue, *, event: str, within_days: float, **_) -> CheckResult:
    cutoff = ctx.known_at - timedelta(days=within_days)
    hits = [e for e in ctx.events if e["event"] == event and parse_ts(e["received_at"]) >= cutoff]
    if not hits:
        return CheckResult(False, f"no {event} in the last {within_days:g} days: recency unprovable")
    return CheckResult(True, f"{event} at {hits[-1]['received_at']}", [_ev(hits[-1])])


def chk_unfinished_series(ctx: ViewerContext, cat: Catalogue, *, idle_days: float, min_episode_reached: int = 2,
                          content_types: list[str] | None = None, **_) -> CheckResult:
    types = set(content_types or ["show", "micro_drama"])
    idle_cut = ctx.known_at - timedelta(days=idle_days)
    cands = []
    for s in ctx.series:
        t = cat.get(s.title_id)
        following = s.furthest_index + 1 >= min_episode_reached or s.content_slug in ctx.completed_slugs
        if (s.content_type in types and s.total_episodes > 1 and s.furthest_index < s.total_episodes - 1
                and s.last_play_at <= idle_cut and following and t is not None and cat.available(t, ctx.known_at)):
            cands.append(s)
    if not cands:
        return CheckResult(False, f"no followed, unfinished series idle for {idle_days:g}+ days")
    s = max(cands, key=lambda x: x.last_play_at)
    return CheckResult(True, f"{s.content_slug}: reached episode {s.furthest_index + 1}/{s.total_episodes}, last played {iso(s.last_play_at)}",
                       [f"event:{s.last_play_event_id}", f"catalogue:{s.title_id}"], "observed",
                       {"content_slug": s.content_slug, "title_id": s.title_id,
                        "next_episode_index": s.furthest_index + 1, "total_episodes": s.total_episodes})


def chk_max_event_days(ctx: ViewerContext, cat: Catalogue, *, event: str, window_days: float, max_days: int, **_) -> CheckResult:
    cutoff = ctx.known_at - timedelta(days=window_days)
    days = {ist_date(parse_ts(e["received_at"])) for e in ctx.events if e["event"] == event and parse_ts(e["received_at"]) >= cutoff}
    if len(days) > max_days:
        return CheckResult(False, f"{event} on {len(days)} days in the last {window_days:g} (cap {max_days})")
    return CheckResult(True, f"{event} on {len(days)} day(s) in the last {window_days:g} (cap {max_days})", [], "observed" if days else "absence")


def chk_no_event_in_window(ctx: ViewerContext, cat: Catalogue, *, event: str, window_days: float, **_) -> CheckResult:
    cutoff = ctx.known_at - timedelta(days=window_days)
    t = ctx.trigger
    prior = [e for e in ctx.events if e["event"] == event and e["event_id"] != t["event_id"]
             and e.get("session_id") != t.get("session_id") and parse_ts(e["received_at"]) >= cutoff]
    if prior:
        return CheckResult(False, f"{event} within the {window_days:g} days before this trigger", [_ev(prior[-1])])
    return CheckResult(True, f"no {event} in the {window_days:g} days before this trigger (current session excluded)",
                       _fact(ctx, "history.window"), "absence")


CHECKS: dict[str, Callable[..., CheckResult]] = {
    "signed_up_within_days": chk_signed_up_within_days, "account_min_age_days": chk_account_min_age_days,
    "no_prior_event": chk_no_prior_event, "subscription_status_in": chk_subscription_status_in,
    "never_paid": chk_never_paid, "recent_event": chk_recent_event, "unfinished_series": chk_unfinished_series,
    "max_event_days": chk_max_event_days, "no_event_in_window": chk_no_event_in_window,
}


def derived_checks(rv: RuleVersion, policy: Policy) -> list[dict[str, Any]]:
    """Checks implied by the structured fields of a goals.json eligibility block."""
    el = rv.eligibility
    out: list[dict[str, Any]] = []
    if "signed_up_within_days" in el:
        out.append({"check": "signed_up_within_days", "days": el["signed_up_within_days"]})
    if "requires_no_prior_event" in el:
        out.append({"check": "no_prior_event", "event": el["requires_no_prior_event"],
                    "require_full_history": bool(policy.history.get("absence_requires_full_history", True))})
    if "subscription_status" in el:
        out.append({"check": "subscription_status_in", "values": list(el["subscription_status"])})
    return out


class GoalEngine:
    def __init__(self, rules: RuleBook, catalogue: Catalogue, policy: Policy) -> None:
        self.rules = rules
        self.catalogue = catalogue
        self.policy = policy

    def evaluate_all(self, ctx: ViewerContext) -> list[Eligibility]:
        return [self.evaluate(gid, ctx) for gid in self.rules.goal_ids()]

    def evaluate(self, goal_id: str, ctx: ViewerContext) -> Eligibility:
        rv = self.rules.current(goal_id)
        if not rv.measurable:
            ev = rv.success.get("event")
            why = ("success event is null / its condition is not an observable signal" if not ev else
                   f"success event {ev!r} never appears in the event stream or simulator output")
            return Eligibility(goal_id, False, f"unmeasurable: {why}; never committed, no proxy invented")
        interp = self.policy.goal_interpretations.get(goal_id)
        checks = derived_checks(rv, self.policy) + (interp.extra_checks if interp else [])
        if not checks:
            return Eligibility(goal_id, False, "eligibility is free text with no reviewed interpretation in the policy; "
                                               "refused rather than guessed")
        reasons, evidence, params, names = [], [], {}, []
        strength = "observed"
        for spec in checks:
            name = spec["check"]
            names.append(name)
            res = CHECKS[name](ctx, self.catalogue, **{k: v for k, v in spec.items() if k != "check"})
            if not res.ok:
                return Eligibility(goal_id, False, res.reason, res.evidence,
                                   [interp.assumption] if interp and interp.assumption else [], checks=names)
            reasons.append(res.reason)
            evidence += [e for e in res.evidence if e not in evidence]
            params.update(res.params)
            if STRENGTH_ORDER[res.strength] < STRENGTH_ORDER[strength]:
                strength = res.strength
        return Eligibility(goal_id, True, "; ".join(reasons), evidence,
                           [interp.assumption] if interp and interp.assumption else [], params, strength, names)

    def ranked_eligible(self, evaluations: list[Eligibility], cooled_down: set[str]) -> list[Eligibility]:
        by_id = {e.goal_id: e for e in evaluations}
        order = list(self.policy.goal_priority) + [g for g in by_id if g not in self.policy.goal_priority]
        return [by_id[g] for g in order if g in by_id and by_id[g].eligible and g not in cooled_down]

    def still_valid(self, plan: dict[str, Any], ctx: ViewerContext) -> tuple[bool, str]:
        """Is an open plan still worth pursuing? Re-checks the goal's preconditions against current facts."""
        if parse_ts(plan["deadline"]) <= ctx.known_at:
            return False, "deadline passed"
        if self.policy.keep_without_recheck(plan["goal_id"]):
            return True, "kept: window still open"
        e = self.evaluate(plan["goal_id"], ctx)
        return (True, "kept: preconditions still hold") if e.eligible else (False, f"preconditions changed: {e.reason}")


# == success matching (shared by the live judge and the final judge) ==============================================

class SuccessJudge:
    """Decides whether an event satisfies a plan's success signal, using the matchers the policy names."""

    def __init__(self, policy: Policy, catalogue: Catalogue) -> None:
        self.policy = policy
        self.cat = catalogue

    def matchers(self, goal_id: str) -> list[dict[str, Any]]:
        return self.policy.success_matchers.get(goal_id, [])

    def aggregate(self, goal_id: str) -> int | None:
        """Minimum distinct days, if the goal needs repeated signals (cannot be judged from one event)."""
        for m in self.matchers(goal_id):
            if m["match"] == "distinct_ist_days":
                return int(m["min_days"])
        return None

    def match(self, plan: dict[str, Any], e: dict[str, Any]) -> bool:
        if e["event"] != plan["success_event"]:
            return False
        props = e.get("properties") or {}
        start = parse_ts(plan["started_at"])
        for m in self.matchers(plan["goal_id"]):
            kind = m["match"]
            if kind == "property_not" and props.get(m["property"]) == m["value"]:
                return False
            if kind == "later_ist_day_than_start" and not ist_date(parse_ts(e["received_at"])) > ist_date(start):
                return False
            if kind == "same_content_as_plan":
                t = self.cat.get(props.get("title_id"))
                if t is None or t.content_slug != (plan.get("params") or {}).get(m["param"]):
                    return False
            if kind == "title_released_within_days_before_start":
                t = self.cat.get(props.get("title_id"))
                if t is None or not (start.date() - timedelta(days=m["days"]) <= t.release_date <= start.date()):
                    return False
        return True

    def judge_window(self, plan: dict[str, Any], events: list[dict[str, Any]]) -> tuple[bool, list[dict[str, Any]]]:
        """(succeeded, matching events) for events received inside [started_at, deadline]."""
        start, end = plan["started_at"], plan["deadline"]
        hits = [e for e in events if start <= e["received_at"] <= end and self.match(plan, e)]
        need = self.aggregate(plan["goal_id"])
        if need:
            return len({ist_date(parse_ts(e["received_at"])) for e in hits}) >= need, hits
        return bool(hits), hits


def plan_deadline(started: datetime, rv: RuleVersion) -> datetime:
    return started + timedelta(hours=rv.window_hours)
