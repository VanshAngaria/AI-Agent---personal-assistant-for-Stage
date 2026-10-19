"""The per-viewer agent: decide, act and keep durable state for one viewer per trigger.

Ownership: every decision for a viewer runs through `AgentService.handle_trigger`, the harness serialises a
viewer's triggers, and the schema allows one open plan per viewer. No other component commits goals.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from oua import SCHEMA_VERSION
from oua.dependencies import (BudgetExceeded, CircuitBreaker, DecisionClock, DependencyTimeout, FaultInjector,
                              ToolClient)
from oua.goals import Eligibility, GoalEngine, RuleBook, RuleVersion, SuccessJudge, plan_deadline
from oua.llm import Advice, Advisor, ModelProvider, build_provider
from oua.pack import Pack, history_window_start
from oua.planner import MessageProposal, PageProposal, Planner
from oua.reconcile import ViewerContext, reconcile
from oua.retrieval import CatalogueIndex
from oua.schemas import Decision
from oua.settings import Settings
from oua.store import Store
from oua.timeutil import ist_date, iso, parse_ts, seconds
from oua.verifier import Verdict, Verifier



@dataclass
class Draft:
    decision_id: str
    trigger: dict[str, Any]
    deliver_at: datetime
    start_at: datetime
    arm: str | None = None
    ctx: ViewerContext | None = None
    goal: dict[str, Any] | None = None
    goal_elig: Eligibility | None = None
    evaluations: list[Eligibility] = field(default_factory=list)
    plan_ops: list[tuple] = field(default_factory=list)
    page: PageProposal | None = None
    message: MessageProposal | None = None
    status: str = "PUBLISHED"
    no_action_reason: str | None = None
    no_action_kind: str | None = None
    checks_passed: list[str] = field(default_factory=list)
    checks_failed: list[str] = field(default_factory=list)
    evidence: dict[str, dict[str, Any]] = field(default_factory=dict)
    assumptions: list[str] = field(default_factory=list)
    advice: Advice | None = None
    rationale: list[str] = field(default_factory=list)
    dropped: list[str] = field(default_factory=list)
    llm: list[Advice] = field(default_factory=list)
    composition: dict[str, Any] | None = None
    retrieval: dict[str, Any] | None = None

    def stop(self, status: str, kind: str, reason: str) -> None:
        self.status, self.no_action_kind, self.no_action_reason = status, kind, reason
        self.page = self.message = None

    def cite(self, ref: str, source: str, observed_at: str | None = None, freshness_s: float | None = None, **extra: Any) -> str:
        if ref not in self.evidence:
            self.evidence[ref] = {"id": ref, "source": source, "observed_at": observed_at, "freshness_s": freshness_s, **extra}
        return ref


class AgentService:
    def __init__(self, settings: Settings, store: Store, pack: Pack, provider: ModelProvider | None = None) -> None:
        self.s = settings
        self.store = store
        self.pack = pack
        self.cat = pack.catalogue
        self.rules = RuleBook(pack.goals)
        for gid in self.rules.goal_ids():
            rv = self.rules.current(gid)
            store.register_rule(gid, rv.version, rv.success, rv.effective_at, rv.source)
        self.policy = pack.policy
        self.page_ttl = timedelta(minutes=int(self.policy.page.get("ttl_min", 120)))
        self.engine = GoalEngine(self.rules, self.cat, self.policy)
        self.judge = SuccessJudge(self.policy, self.cat)
        rc = self.policy.retrieval
        self.index = CatalogueIndex(self.cat, rc.get("field_boost", {}), **rc.get("bm25", {}))
        self.planner = Planner(self.cat, pack.interventions, self.policy, self.index)
        self.arms = {r["user_id"]: r["arm"] for r in store.conn.execute("SELECT user_id, arm FROM assignments")}
        self.verifier = Verifier(self.cat, pack.interventions, pack.pricing, self.arms, settings, self.policy)
        self.faults = FaultInjector()
        self.store_breaker = CircuitBreaker(settings.breaker_cooldown_s)
        self.model_breaker = CircuitBreaker(settings.breaker_cooldown_s)
        self.advisor = Advisor(provider or build_provider(settings.llm_provider, pack.model_faults(), model=settings.llm_model,
                                                          effort=settings.llm_effort, mock_latency_s=settings.model_latency_s))
        self.cache: dict[str, tuple[list, list, str]] = {}
        self.history_start = history_window_start(store)
        self.snapshot_at = store.conn.execute("SELECT MAX(snapshot_at) FROM profile_rows").fetchone()[0]
        self._d = store.conn.execute("SELECT COUNT(*) FROM decisions").fetchone()[0]
        self._a = store.conn.execute("SELECT COUNT(*) FROM actions").fetchone()[0]
        self._restore_from_state()

    def _restore_from_state(self) -> None:
        """Re-apply durable control history (rule versions, withdrawals, fault windows) after a restart."""
        for c in self.store.control_log():
            p = c["payload"]
            if c["type"] == "goal_rule_corrected":
                self.rules.correct_success(p["goal_id"], p["new_success"], p["at"], "replay/control_events.jsonl")
            elif c["type"] == "title_withdrawn":
                self.cat.withdraw(p["title_id"], parse_ts(p["at"]), p.get("reason", ""))
            elif c["type"] in ("dependency_unavailable", "dependency_degraded"):
                self.faults.add_control(p)

    # ======================================================================================================
    # Triggers
    # ======================================================================================================

    def handle_trigger(self, trigger: dict[str, Any], start_at: datetime | None = None) -> dict[str, Any]:
        deliver_at = parse_ts(trigger.get("deliver_at") or trigger["received_at"])
        start_at = start_at or deliver_at
        first, existing = self.store.claim_trigger(trigger["event_id"], trigger["user_id"], iso(deliver_at))
        self.store.ingest_event({k: v for k, v in trigger.items() if k != "deliver_at"}, "replay_trigger")
        if not first and existing is not None:
            return {"redelivery": True, "event_id": trigger["event_id"], "decision_id": existing,
                    "deliver_at": iso(deliver_at), "viewer_id": trigger["user_id"]}

        cpu0 = time.perf_counter()
        clock = DecisionClock(start_at)
        tools = ToolClient(self.s, self.faults, clock, self.store_breaker, self.model_breaker, trigger["event_id"])
        queued_s = seconds(deliver_at, start_at)
        # The page must land before the viewer reaches home: model calls may only use what is left of that target.
        tools.model_deadline_s = max(0.0, self.s.page_target_s - queued_s)
        if queued_s > self.s.shed_model_after_queue_s:
            tools.shed_reason = f"this decision waited {queued_s:.1f}s in the queue (> {self.s.shed_model_after_queue_s:.0f}s)"
        self._d += 1
        d = Draft(f"D-{self._d:06d}", trigger, deliver_at, start_at)
        try:
            self._decide(d, clock, tools, cpu0)
        except BudgetExceeded as exc:
            d.stop("FAILED_SAFE", "budget", f"call budget exhausted ({exc}); publishing nothing is the safe fallback")
        return self._finalise(d, clock, tools, time.perf_counter() - cpu0)

    def _decide(self, d: Draft, clock: DecisionClock, tools: ToolClient, cpu0: float) -> None:
        t = d.trigger
        uid = t["user_id"]
        d.cite(f"event:{t['event_id']}", "event", t["received_at"], 0.0, note=f"trigger {t['event']}")

        # 0. Experiment arm (immutable assignment; fail closed when missing).
        d.arm = self.arms.get(uid)
        d.cite(f"arm:{uid}", "replay/arms.csv", note=f"arm={d.arm}")
        if d.arm is None:
            d.stop("FAILED_SAFE", "no_arm", "viewer has no randomized arm; acting could contaminate the holdout, so fail closed")
            return

        # 1. Understand the viewer (reads go through the budgeted, fault-aware client).
        known_at = clock.start
        stale = False
        try:
            rows = tools.tool("profile.read", lambda: self.store.profile_rows(uid))
            events = tools.tool("events.read", lambda: self.store.events_for(uid, iso(known_at)))
            self.cache[uid] = (rows, events, iso(known_at))
        except DependencyTimeout as exc:
            cached = self.cache.get(uid)
            if cached is None:
                d.stop("FAILED_SAFE", "data_store", f"user data store failed ({exc}) and this agent holds no earlier "
                       "snapshot of the viewer; acting without facts is unsafe")
                return
            rows, events, cached_at = cached
            if all(e["event_id"] != t["event_id"] for e in events):
                events = events + [self.store.get_event(t["event_id"])]
            stale = True
            d.assumptions.append(f"user data store unavailable ({exc}); acting on the snapshot cached at {cached_at}")
        plan = tools.tool("plan.read", lambda: self.store.open_plan(uid), dependency=None)
        lessons = tools.tool("lessons.read", lambda: self.store.lessons(uid, iso(known_at)), dependency=None)
        ctx = reconcile(trigger=t, known_at=known_at, profile_rows=rows, events=events, catalogue=self.cat,
                        history_start=self.history_start, stale_after_s=float(self.policy.trigger['stale_after_s']),
                        stale_snapshot=stale, snapshot_at=self.snapshot_at)
        d.ctx = ctx
        for key in ("subscription_status", "dialect.app_setting", "trigger.live_visit", "profile.present"):
            self._cite_fact(d, key)
        if not ctx.timing.live_visit:
            d.stop("NO_ACTION", "stale_trigger", f"late trigger: {ctx.timing.reason}. Ingested as history only; "
                   "no page (it would never be seen this visit) and no change to the plan")
            return

        # 2. Plan maintenance: keep, close or revise the single active plan.
        if plan is not None:
            ok, why = self.engine.still_valid(plan, ctx)
            if not ok:
                status = "inconclusive" if why == "deadline passed" else "superseded"
                note = ("deadline passed during replay; the final judge re-checks it with complete outcome data"
                        if status == "inconclusive" else why)
                d.plan_ops.append(("close", plan["plan_id"], status, note))
                d.rationale.append(f"closed plan {plan['plan_id']} ({plan['goal_id']}): {note}")
                plan = None

        # 3. Choose a goal (deterministic eligibility; the model may only choose among eligible goals).
        d.evaluations = self.engine.evaluate_all(ctx)
        by_id = {e.goal_id: e for e in d.evaluations}
        if plan is not None:
            rv = self.rules.get(plan["rule_version"])
            d.goal_elig = by_id.get(plan["goal_id"])
            d.advice = self.advisor.skipped("choose_goal", "an active plan is still valid; no new goal choice to make")
            d.goal = self._goal_dict(rv, parse_ts(plan["started_at"]), parse_ts(plan["deadline"]), plan["plan_id"], kept=True,
                                     params=plan["params"])
            d.rationale.append(f"kept active plan {plan['plan_id']} ({plan['goal_id']}) started {plan['started_at']}")
        else:
            cutoff = known_at - timedelta(days=float(self.policy.plans.get("failed_goal_cooldown_days", 7)))
            cooled = {l["kind"].split(":", 1)[1] for l in lessons
                      if l["kind"].startswith("goal_failed:") and parse_ts(l["at"]) >= cutoff}
            ranked = self.engine.ranked_eligible(d.evaluations, cooled)
            if not ranked:
                d.stop("NO_ACTION", "no_goal", "no measurable goal the viewer is eligible for yet: " + "; ".join(
                    f"{e.goal_id} {e.reason}" for e in d.evaluations))
                return
            chosen = ranked[0]
            gcfg = self.policy.llm.get("choose_goal", {})
            if gcfg.get("enabled", True) and len(ranked) >= int(gcfg.get("only_when_eligible_goals_at_least", 2)):
                d.advice = self.advisor.choose_goal(
                    tools, t["event_id"],
                    [{"goal_id": e.goal_id, "name": self.policy.label(e.goal_id), "why_eligible": e.reason} for e in ranked],
                    {e.goal_id: e.reason for e in d.evaluations if not e.eligible},
                    {"status": ctx.status, "plays": len(ctx.plays), "completions": len(ctx.completions),
                     "dialect": ctx.dialect, "signup_age_days": None if ctx.signup_at is None else
                     round(seconds(ctx.signup_at, known_at) / 86400, 1)})
                if d.advice.outcome in ("agreed", "chose_other_eligible"):
                    chosen = next(e for e in ranked if e.goal_id == d.advice.goal_id)
            else:
                d.advice = self.advisor.skipped("choose_goal", "fewer eligible goals than the policy threshold; a model call cannot reduce uncertainty")
            rv = self.rules.current(chosen.goal_id)
            started = clock.now(time.perf_counter() - cpu0)
            deadline = plan_deadline(started, rv)
            d.goal_elig = chosen
            d.goal = self._goal_dict(rv, started, deadline, None, kept=False, params=chosen.params)
            d.plan_ops.append(("create", {
                "viewer_id": uid, "arm": d.arm, "goal_id": rv.goal_id, "rule_version": rv.version,
                "success_event": rv.success["event"], "success_condition": rv.success.get("condition", ""),
                "window_hours": rv.window_hours, "params": chosen.params, "started_at": iso(started),
                "deadline": iso(deadline), "decision_id": d.decision_id}))
            d.rationale.append(f"committed {rv.goal_id} ({rv.name}): {chosen.reason}")
        if d.goal_elig:
            for ref in d.goal_elig.evidence:
                self._cite_ref(d, ref)
            d.assumptions.extend(d.goal_elig.assumptions)
        rv = self.rules.get(d.goal["rule_version"])
        d.cite(f"goal_rule:{rv.version}", rv.source, rv.effective_at, None, success=rv.success)

        # 4. Holdout: the goal is a shadow goal for measurement only.
        if d.arm == "holdout":
            d.stop("NO_ACTION", "holdout", "holdout arm: no action of any kind (messaging policy rule 5); "
                   "the goal is recorded as a shadow goal so both arms are judged the same way")
            d.checks_passed.append("holdout_respected")
            return
        if ctx.dialect is None:
            d.stop("NO_ACTION", "no_dialect", f"the app is set to {t.get('app_dialect')!r}, which has no catalogue content; "
                   "a page would not be shown and any title would be in a dialect the viewer does not see")
            return

        # 5. Plan the intervention: retrieve candidates, let the model compose within them, check grounding.
        d.llm.append(d.advice)
        now = clock.now(time.perf_counter() - cpu0)
        recent = tools.tool("actions.read", lambda: self.store.actions_for(uid), dependency=None)
        live_page = next((a for a in reversed(recent) if a["type"] == "home_page" and not a.get("superseded_by")
                          and parse_ts(a["published_at"]) + self.page_ttl > now), None)
        params = d.goal.get("params") or {}
        message_days = tools.tool("ledger.read", lambda: self.store.message_days(uid), dependency=None)
        plan_has_message = any(a["type"] in ("push", "whatsapp") and a.get("plan_id") == d.goal.get("plan_id")
                               and d.goal.get("plan_id") is not None for a in recent)
        follow_up_possible = bool(self.planner.follow_up_config(d.goal["id"])) and not plan_has_message and not stale
        page_still_live = None
        page = None
        model_declined_follow_up = False
        margin = timedelta(minutes=float(self.policy.page.get("refresh_margin_min", 30)))
        if (live_page and live_page.get("dialect") == ctx.dialect and live_page.get("goal_id") == d.goal["id"]
                and parse_ts(live_page["published_at"]) + self.page_ttl - now > margin):
            d.rationale.append(f"page {live_page['action_id']} for this goal and dialect is still live until "
                               f"{iso(parse_ts(live_page['published_at']) + self.page_ttl)}; not republished")
            d.checks_passed.append("no_duplicate_page")
            page_still_live = live_page
        else:
            cands = tools.tool("catalogue.retrieve",
                               lambda: self.planner.candidates(ctx, d.goal["id"], params, now + timedelta(seconds=1)),
                               dependency=None)
            if cands is None:
                d.rationale.append(f"no page: no promotable titles in {ctx.dialect}")
            else:
                d.retrieval = {"query_terms": cands.query.top_terms(12), "searches_used": len(cands.query.searches),
                               "plays_used": len(cands.query.watched), "hits": [h.as_dict() for h in cands.hits[:8]]}
                page, follow = self._compose(d, tools, ctx, cands, follow_up_possible)
                model_declined_follow_up = follow_up_possible and not follow
                if page is None:
                    d.rationale.append("no page: no row could keep 3 or more valid titles")
                else:
                    d.rationale.extend(page.rationale)
                    self._cite_retrieval(d, page, cands)
        if stale:
            message, mwhy = None, "no message: consent and caps cannot be confirmed while the data store is degraded"
        elif model_declined_follow_up:
            message, mwhy = None, "no message: the model judged a follow-up unnecessary for this viewer (it may decline one, never add one)"
        else:
            message, mwhy = self.planner.plan_message(ctx, d.goal["id"], params, parse_ts(d.goal["deadline"]), page,
                                                      message_days, lessons, plan_has_message, now)
        d.rationale.append(mwhy if message is None else message.rationale)
        if message is not None and message.channel == "push":
            self._push_copy(d, tools, message, ctx)

        # 6. Verify independently, downgrade on failure.
        for attempt in range(2):
            verdict = self._verify(d, tools, rows, events, page, message, message_days, cpu0, clock)
            if attempt == 0 and page is not None and verdict.bad_titles and not verdict.blocking:
                page = self._repair_page(page, verdict.bad_titles, d)
                continue
            break
        d.checks_passed.extend(verdict.passed)
        d.checks_failed.extend(verdict.failed)
        if verdict.blocking:
            d.stop("NO_ACTION", "verifier_blocked", "independent verifier blocked every action: " + "; ".join(verdict.failed))
            return
        if page is not None and not verdict.page_ok:
            d.dropped.append("home_page (failed verification)")
            page = None
        if (message is not None and not verdict.message_ok and message.channel == "push"
                and message.text != self.planner.template_copy(message, ctx)):
            message.text = self.planner.template_copy(message, ctx)
            d.rationale.append("model copy failed verification; the policy template copy was verified instead")
            verdict = self._verify(d, tools, rows, events, page, message, message_days, cpu0, clock)
            d.checks_passed.extend(f"retry:{c}" for c in verdict.passed if c.startswith("message."))
            d.checks_failed.extend(f"retry:{c}" for c in verdict.failed)
        if message is not None and not verdict.message_ok:
            d.dropped.append(f"{message.channel} (failed verification)")
            message = None
        if page is None and message is None and page_still_live is not None:
            until = iso(parse_ts(page_still_live["published_at"]) + self.page_ttl)
            d.stop("NO_ACTION", "page_already_live", f"page {page_still_live['action_id']} for this goal and dialect is live "
                   f"until {until}; republishing it would be a duplicate action, and no follow-up message is warranted ({mwhy})")
            return
        if page is None and message is None:
            d.stop("NO_ACTION", "nothing_to_do", "nothing proportionate to publish: " + "; ".join(d.rationale[-3:] + d.dropped))
            return
        d.page, d.message = page, message
        for tid in (page.title_ids() if page else []) + ([message.title_id] if message else []):
            title = self.cat.get(tid)
            d.cite(f"catalogue:{tid}", "data/catalogue.csv", iso(title.available_from), None,
                   dialect=title.dialect, available_until=iso(title.available_until))
        d.cite("policy:interventions", "config/interventions.json", None, None, version=self.pack.interventions.get("version"))
        d.cite(self.policy.ref, "config/agent_policy.json", None, None, version=self.policy.version)

    def _compose(self, d: Draft, tools: ToolClient, ctx: ViewerContext, cands, follow_up_possible: bool):
        """Model composes the page from the retrieved candidates; grounding is checked; rules are the fallback."""
        if not self.policy.llm.get("compose_page", {}).get("enabled", True):
            d.llm.append(self.advisor.skipped("compose_page", "disabled in policy"))
            d.composition = {"by": "rules", "why": "model composition disabled in policy"}
            return self.planner.deterministic(cands), follow_up_possible
        payload = self.planner.compose_payload(ctx, d.goal, cands, follow_up_possible)
        adv = self.advisor.compose_page(tools, d.trigger["event_id"], payload)
        d.llm.append(adv)
        if adv.data is not None:
            page, problems = self.planner.from_model(cands, adv.data)
            if page is not None:
                if problems:
                    adv.outcome = "used_after_grounding_fixes"
                d.composition = {"by": f"model ({adv.provider}:{adv.model})", "rationale": adv.data.get("rationale", ""),
                                 "grounding_problems": problems, "candidates_offered": len(cands.ids())}
                return self.planner.finalize(cands, page, f"the model ({adv.provider})"), bool(adv.data.get("send_follow_up"))
            adv.outcome = "ungrounded"
            adv.detail = "composition referenced nothing usable: " + "; ".join(problems[:4])
        d.composition = {"by": "rules", "why": f"fallback: model {adv.outcome} ({adv.detail[:160]})"}
        return self.planner.deterministic(cands), follow_up_possible

    def _cite_retrieval(self, d: Draft, page: PageProposal, cands) -> None:
        hits = {h.title_id: h for h in cands.hits}
        for tid in page.title_ids():
            h = hits.get(tid)
            if h is not None:
                d.cite(f"retrieval:{tid}", "bm25 over the catalogue, queried with this viewer's searches and plays", None, None,
                       score=round(h.score, 3), matched_terms=h.matched, query_sources=h.sources)

    def _push_copy(self, d: Draft, tools: ToolClient, m: MessageProposal, ctx: ViewerContext) -> None:
        fallback = self.planner.template_copy(m, ctx)
        if self.policy.llm.get("write_copy", {}).get("enabled", True):
            adv = self.advisor.write_copy(tools, d.trigger["event_id"], self.planner.copy_payload(m, ctx, d.goal))
        else:
            adv = self.advisor.skipped("write_copy", "disabled in policy")
        d.llm.append(adv)
        if adv.copy:
            m.text = adv.copy
            d.rationale.append("push copy drafted by the model; the verifier checks length, phrases, prices and the title")
        else:
            m.text = fallback
            d.rationale.append(f"push copy from the policy template (model {adv.outcome})")

    def _verify(self, d: Draft, tools: ToolClient, rows, events, page, message, message_days, cpu0, clock) -> Verdict:
        projected = clock.now(time.perf_counter() - cpu0)
        return self.verifier.verify(
            viewer_id=d.trigger["user_id"], trigger=d.trigger, profile_rows=rows, events=events,
            goal_id=d.goal["id"], rule=self.rules.get(d.goal["rule_version"]), evidence=list(d.evidence),
            page=page, message=message, publish_at=projected + timedelta(seconds=1),
            deadline=parse_ts(d.goal["deadline"]), message_days=message_days, tool_calls=tools.tool_calls,
            model_calls=tools.model_calls, latency_s=seconds(d.deliver_at, projected))

    @staticmethod
    def _repair_page(page: PageProposal, bad: dict[str, str], d: Draft) -> PageProposal | None:
        rows = []
        for r in page.rows:
            ids = [t for t in r["title_ids"] if t not in bad]
            if len(ids) >= 3:
                rows.append({"row_type": r["row_type"], "title_ids": ids})
        hero = page.hero_title_id if page.hero_title_id not in bad else None
        d.rationale.append(f"removed {len(bad)} title(s) the verifier rejected: " + "; ".join(f"{k} ({v})" for k, v in bad.items()))
        if not rows and not hero:
            return None
        return PageProposal(page.dialect, hero, rows, page.rationale)

    # ======================================================================================================
    # Finalise and persist
    # ======================================================================================================

    def _finalise(self, d: Draft, clock: DecisionClock, tools: ToolClient, cpu_s: float) -> dict[str, Any]:
        published_at = clock.now(cpu_s)
        latency_s = seconds(d.deliver_at, published_at)
        if d.status == "PUBLISHED" and latency_s > self.s.failure_after_s:
            d.stop("FAILED_LATE", "late", f"decision would publish {latency_s:.0f}s after the trigger (> {self.s.failure_after_s:.0f}s); not published")
        if d.status == "PUBLISHED" and d.page is None and d.message is None:
            d.stop("NO_ACTION", "nothing_to_do", "no action survived planning")
        actions = self._actions(d, published_at) if d.status == "PUBLISHED" else []
        decision = self._record(d, actions, tools, published_at, latency_s, cpu_s)
        Decision.model_validate(decision)  # schema invariants; a violation is a bug, so fail loudly

        with self.store.tx() as conn:
            plan_id = d.goal.get("plan_id") if d.goal else None
            for op in d.plan_ops:
                if op[0] == "close":
                    self.store.close_plan(conn, op[1], op[2], iso(clock.start), op[3])
                else:
                    plan_id = self.store.create_plan(conn, op[1])
            if d.goal is not None:
                d.goal["plan_id"] = decision["goal"]["plan_id"] = plan_id
            for a in actions:
                a["plan_id"] = plan_id
            self.store.save_decision(conn, decision)
            if d.ctx is not None:
                self.store.save_facts(conn, d.ctx.viewer_id, d.decision_id, iso(clock.start), d.ctx.facts_list())
            for a in actions:
                self.store.save_action(conn, a, self._message_day(a))
        return {"redelivery": False, "decision": decision, "actions": actions, "published_at": published_at}

    @staticmethod
    def _message_day(a: dict[str, Any]) -> str | None:
        if a["type"] not in ("push", "whatsapp"):
            return None
        return str(ist_date(parse_ts(a["send_at"])))

    def _actions(self, d: Draft, published_at: datetime) -> list[dict[str, Any]]:
        out = []
        base = {"decision_id": d.decision_id, "user_id": d.trigger["user_id"], "trigger_event_id": d.trigger["event_id"],
                "published_at": iso(published_at), "goal_id": d.goal["id"]}
        if d.page is not None:
            self._a += 1
            out.append({"action_id": f"A-{self._a:06d}", **base, "type": "home_page", "dialect": d.page.dialect,
                        "hero_title_id": d.page.hero_title_id, "rows": d.page.rows})
        if d.message is not None:
            m = d.message
            self._a += 1
            a = {"action_id": f"A-{self._a:06d}", **base, "type": m.channel, "send_at": iso(m.send_at), "title_id": m.title_id,
                 "dialect": d.ctx.dialect}
            if m.channel == "push":
                a.update({"text": m.text, "deeplink": m.deeplink})
            else:
                a.update({"template_id": m.template_id, "variables": m.variables})
            out.append(a)
        return out

    def _record(self, d: Draft, actions: list[dict[str, Any]], tools: ToolClient, published_at: datetime,
                latency_s: float, cpu_s: float) -> dict[str, Any]:
        ctx, t = d.ctx, d.trigger
        timing = ctx.timing if ctx else None
        if d.status == "PUBLISHED":
            action = "HOME_PAGE" if d.page else d.message.channel.upper()
        else:
            action = "NO_ACTION"
        ids = (d.page.title_ids() if d.page else []) + ([d.message.title_id] if d.message else [])
        content = [f"title:{x}" for x in dict.fromkeys(ids)]  # a title can be both the hero and the message subject
        conf, conf_reason = self._confidence(d)
        dialect_conflicts = []
        if ctx:
            for c in ctx.conflicts:
                if c.fact == "dialect":
                    dialect_conflicts = [f"{v['source']}:{v['value']}" for v in c.values if v["source"] != "app_setting"]
        decision = {
            "schema_version": SCHEMA_VERSION,
            "decision_id": d.decision_id,
            "viewer_id": t["user_id"],
            "arm": d.arm,
            "trigger": {
                "event": t["event"], "event_id": t["event_id"], "received_at": t["received_at"],
                "deliver_at": iso(d.deliver_at), "processing_started_at": iso(d.start_at),
                "occurred_at": t.get("occurred_at"), "sent_at": t.get("sent_at"),
                "event_age_s": None if not timing or timing.event_age_s is None else round(timing.event_age_s, 1),
                "buffered_s": None if not timing or timing.buffered_s is None else round(timing.buffered_s, 1),
                "clock_skew_suspected": bool(timing and timing.clock_skew_suspected),
                "live_visit": bool(timing and timing.live_visit),
                "session_id": t.get("session_id"),
            },
            "goal": None if d.goal is None else {k: v for k, v in d.goal.items() if k != "params"} | {"params": d.goal.get("params") or {}},
            "action": action,
            "actions": [{"action_id": a["action_id"], "type": a["type"], "send_at": a.get("send_at"),
                         "published_at": a["published_at"]} for a in actions],
            "no_action_reason": d.no_action_reason,
            "no_action_kind": d.no_action_kind,
            "dialect": ({"used": ctx.dialect, "source": "app_setting", "conflicts": dialect_conflicts} if ctx else
                        {"used": d.page.dialect if d.page else None, "source": "dialect of the live page being repaired",
                         "conflicts": []}),
            "content": content,
            "confidence": conf,
            "confidence_reason": conf_reason,
            "evidence": list(d.evidence),
            "evidence_details": list(d.evidence.values()),
            "assumptions": d.assumptions + self._standing_assumptions(d),
            "conflicting_evidence": ctx.conflicts_list() if ctx else [],
            "data_quality": ctx.data_quality if ctx else [],
            "goal_evaluations": [e.as_dict() for e in d.evaluations],
            "rationale": d.rationale,
            "dropped_by_verifier": d.dropped,
            "checks": {"passed": d.checks_passed, "failed": d.checks_failed},
            "model": d.advice.as_dict() if d.advice else {"task": "choose_goal", "consulted": False, "outcome": "skipped",
                                                          "detail": "decision ended before a model could help"},
            "llm": {"provider": self.advisor.provider.name, "model": self.advisor.provider.model,
                    "tasks": [a.as_dict() for a in d.llm if a is not None]},
            "composition": d.composition,
            "retrieval": d.retrieval,
            "policy_version": self.policy.version,
            "budget": {k: v for k, v in tools.summary().items()},
            "decided_in_ms": int(round(seconds(d.start_at, published_at) * 1000)),
            "queue_wait_ms": int(round(seconds(d.deliver_at, d.start_at) * 1000)),
            "latency_ms": int(round(latency_s * 1000)),
            "cpu_ms": round(cpu_s * 1000, 2),
            "published_at": iso(published_at) if actions else None,
            "status": d.status,
        }
        return decision

    def _standing_assumptions(self, d: Draft) -> list[str]:
        out = []
        ctx = d.ctx
        if ctx is None:
            return out
        if ctx.profile is not None:
            out.append(f"profile snapshot (updated {ctx.profile.get('updated_at')}) still describes the viewer except where "
                       "newer events say otherwise")
        if d.page is not None:
            out.append(f"the viewer's app stays on {ctx.dialect} while the page is live ({self.page_ttl.seconds // 60} min)")
            out.append("the page reaches the viewer before they land on home (published within seconds of the open)")
        if d.message is not None:
            out.append(f"consent at send time ({iso(d.message.send_at)}) is unchanged from now; delivery is only "
                       "confirmed by a message_delivered event")
        return out

    def _confidence(self, d: Draft) -> tuple[float, str]:
        """Documented heuristic (not calibrated): how well-supported the decision is by current evidence.

        Weights live in config/agent_policy.json (confidence.terms); each applied term is spelled out in the reason.
        """
        cfg = self.policy.confidence
        w = self.policy.conf_terms
        fixed = cfg.get("abstention", {})
        if d.no_action_kind in fixed:
            c, why = fixed[d.no_action_kind]
            return float(c), f"Abstention confidence (policy): {why}"
        ctx = d.ctx
        if ctx is None:
            return 0.3, "Heuristic: no viewer context could be assembled"
        terms: list[str] = []
        c = float(cfg.get("base", 0.5))

        def add(delta: float, why: str) -> None:
            nonlocal c
            c += delta
            terms.append(f"{delta:+.2f} {why}")

        if ctx.profile is not None:
            age_h = seconds(parse_ts(ctx.profile["updated_at"]), ctx.known_at) / 3600
            add(w["profile_fresh"] if age_h < cfg.get("profile_fresh_hours", 72) else 0.0, f"profile snapshot {age_h:.0f}h old")
        else:
            add(w["no_profile"], "no profile row")
        if ctx.stale_snapshot:
            add(w["stale_snapshot"], "acting on a cached snapshot (data store degraded)")
        if d.goal_elig is not None:
            s_ = d.goal_elig.evidence_strength
            add(w[f"evidence_{s_}"], f"goal eligibility rests on {s_} evidence")
        if ctx.conflicts:
            n = len(ctx.conflicts)
            add(w["per_conflict"] * min(int(w["max_conflicts_counted"]), n), f"{n} conflicting source(s) resolved by precedence")
        if len(ctx.plays) >= w["rich_history_min_plays"]:
            add(w["rich_history"], f"behavioural history ({len(ctx.plays)} plays)")
        elif not ctx.plays:
            add(w["no_history"], "no play history to personalise from")
        if d.advice and d.advice.consulted:
            if d.advice.outcome == "agreed":
                add(w["model_agreed"], "model's goal choice agreed with the rules")
            elif d.advice.outcome == "rejected_unsupported":
                add(w["model_rejected"], "model made an unsupported claim (rejected)")
        if d.retrieval and d.retrieval.get("hits"):
            add(w["retrieval_grounded"], "content grounded in retrieval against the viewer's own signals")
        if d.dropped:
            add(w["verifier_dropped"], "verifier dropped part of the plan")
        c = round(max(0.05, min(0.95, c)), 2)
        return c, "Heuristic, not calibrated (weights in agent_policy.json): " + "; ".join(terms)

    # -- evidence helpers -------------------------------------------------------------------------------------

    def _cite_fact(self, d: Draft, key: str) -> None:
        f = d.ctx.fact(key) if d.ctx else None
        if f is not None:
            d.cite(f"fact:{key}", f.source, f.observed_at, f.freshness_s, source_id=f.source_id, value=f.value, note=f.note)

    def _cite_ref(self, d: Draft, ref: str) -> None:
        kind, _, rest = ref.partition(":")
        if kind == "fact":
            self._cite_fact(d, rest)
        elif kind == "event":
            e = next((x for x in (d.ctx.events if d.ctx else []) if x["event_id"] == rest), None)
            if e is not None:
                d.cite(ref, "event", e["received_at"], round(seconds(parse_ts(e["received_at"]), d.ctx.known_at), 1), event=e["event"])
            else:
                d.cite(ref, "event", note="not found in viewer events")
        elif kind == "catalogue":
            title = self.cat.get(rest)
            d.cite(ref, "data/catalogue.csv", iso(title.available_from) if title else None)
        else:
            d.cite(ref, kind)

    def _goal_dict(self, rv: RuleVersion, started: datetime, deadline: datetime, plan_id: int | None, *, kept: bool,
                   params: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": rv.goal_id, "name": rv.name, "started_at": iso(started), "deadline": iso(deadline),
            "success_signal": {"event": rv.success["event"], "condition": rv.success.get("condition"),
                               "window_hours": rv.window_hours,
                               "judged_by": f"succeeded if a {rv.success['event']} event ({rv.success.get('condition')}) is "
                                            f"received between started_at and deadline; otherwise failed, or inconclusive "
                                            "if the observation period ends first"},
            "rule_version": rv.version, "plan_id": plan_id, "kept_from_previous_decision": kept, "params": params,
        }

    # ======================================================================================================
    # Live events and control events
    # ======================================================================================================

    def on_live_event(self, e: dict[str, Any]) -> dict[str, Any]:
        ev = {k: v for k, v in e.items() if k != "deliver_at"}
        first = self.store.ingest_event(ev, "replay_live")
        result: dict[str, Any] = {"event_id": e["event_id"], "duplicate": not first}
        if not first:
            return result
        uid = e["user_id"]
        at = e["received_at"]
        if e["event"] == "notification_opt_out":
            ch = (e.get("properties") or {}).get("channel")
            self.store.add_lesson(uid, at, f"opted_out:{ch}", "viewer opted out; never message on this channel again", [f"event:{e['event_id']}"])
        plan = self.store.open_plan(uid)
        if plan is None:
            return result
        if (self.judge.aggregate(plan["goal_id"]) is None and plan["started_at"] <= at <= plan["deadline"]
                and self.judge.match(plan, e)):
            sent, occ = parse_ts(e.get("sent_at")), parse_ts(e.get("occurred_at"))
            buffered = seconds(occ, sent) if sent and occ else 0.0
            est_occurred = parse_ts(at) - timedelta(seconds=max(0.0, buffered))
            with self.store.tx() as conn:
                if est_occurred < parse_ts(plan["started_at"]):
                    self.store.close_plan(conn, plan["plan_id"], "inconclusive", at,
                                          f"signal {e['event_id']} happened on the device before the goal started "
                                          f"(buffered {buffered:.0f}s); not attributable to this plan", e["event_id"])
                    result["plan_closed"] = "inconclusive"
                else:
                    self.store.close_plan(conn, plan["plan_id"], "succeeded", at,
                                          f"live {e['event']} received inside the goal window", e["event_id"])
                    result["plan_closed"] = "succeeded"
            self.store.add_lesson(uid, at, f"goal_{result['plan_closed']}:{plan['goal_id']}",
                                  f"{plan['goal_id']} closed by live event {e['event']}", [f"event:{e['event_id']}"])
        elif e["event"] in ("purchase_completed", "subscription_renewed", "subscription_lapsed"):
            with self.store.tx() as conn:
                self.store.plan_change(conn, plan["plan_id"], uid, at, "facts_changed",
                                       {"event": e["event"], "event_id": e["event_id"],
                                        "effect": "plan preconditions are re-checked at this viewer's next trigger"})
            result["plan_flagged"] = plan["plan_id"]
        return result

    def on_control(self, c: dict[str, Any]) -> dict[str, Any]:
        at = parse_ts(c["at"])
        affected: dict[str, Any] = {}
        if c["type"] == "title_withdrawn":
            self.cat.withdraw(c["title_id"], at, c.get("reason", ""))
            pages = self.store.live_pages_with_title(c["title_id"], iso(at - self.page_ttl), iso(at))
            affected = {"live_pages": [p["action_id"] for p in pages], "replacements": []}
            for p in pages:
                rep = self._replace_page(p, c, at)
                affected["replacements"].append(rep)
        elif c["type"] == "goal_rule_corrected":
            rv = self.rules.correct_success(c["goal_id"], c["new_success"], c["at"], "replay/control_events.jsonl")
            self.store.register_rule(rv.goal_id, rv.version, rv.success, rv.effective_at, rv.source)
            plans = self.store.open_plans_for_goal(c["goal_id"])
            with self.store.tx() as conn:
                for p in plans:
                    self.store.update_plan_rule(conn, p["plan_id"], rv.version, rv.success["event"],
                                                rv.success.get("condition", ""), c["at"],
                                                {"from": p["rule_version"], "to": rv.version,
                                                 "old_success_event": p["success_event"], "new_success_event": rv.success["event"],
                                                 "note": c.get("note")})
            affected = {"new_version": rv.version, "open_plans_updated": [p["plan_id"] for p in plans],
                        "viewers": sorted({p["viewer_id"] for p in plans}), "other_viewers_reprocessed": 0}
        elif c["type"] in ("dependency_unavailable", "dependency_degraded"):
            w = self.faults.add_control(c)
            affected = {"dependency": w.dependency, "window": [iso(w.start), iso(w.end)], "mode": w.kind}
        else:
            affected = {"ignored": f"unknown control type {c['type']!r}"}
        self.store.log_control(c["at"], c["type"], c, affected)
        return affected

    def _replace_page(self, page: dict[str, Any], c: dict[str, Any], at: datetime) -> dict[str, Any]:
        """Republish a live page without the withdrawn title (the platform shows the newest live page)."""
        uid = page["user_id"]
        plan = self.store.open_plan(uid)
        if plan is None or plan.get("goal_id") != page.get("goal_id"):
            return {"page": page["action_id"], "result": "not replaced: its plan is no longer open"}
        removed = c["title_id"]
        rows = [{"row_type": r["row_type"], "title_ids": [x for x in r["title_ids"] if x != removed]} for r in page["rows"]]
        rows = [r for r in rows if len(r["title_ids"]) >= 3]
        hero = page.get("hero_title_id") if page.get("hero_title_id") != removed else None
        proposal = PageProposal(page["dialect"], hero, rows, [f"replacement for {page['action_id']}: {removed} withdrawn"])
        pseudo_trigger = {"event": "control:title_withdrawn", "event_id": f"control:{c['at']}:{page['action_id']}",
                          "user_id": uid, "received_at": c["at"], "deliver_at": c["at"], "app_dialect": page["dialect"],
                          "properties": {}, "session_id": None}
        self.store.claim_trigger(pseudo_trigger["event_id"], uid, c["at"])
        clock = DecisionClock(at)
        tools = ToolClient(self.s, self.faults, clock, self.store_breaker, self.model_breaker, pseudo_trigger["event_id"])
        self._d += 1
        d = Draft(f"D-{self._d:06d}", pseudo_trigger, at, at, arm=self.arms.get(uid))
        rv = self.rules.get(plan["rule_version"])
        d.goal = self._goal_dict(rv, parse_ts(plan["started_at"]), parse_ts(plan["deadline"]), plan["plan_id"], kept=True, params=plan["params"])
        d.cite(f"control:{c['type']}@{c['at']}", "replay/control_events.jsonl", c["at"], 0.0, title_id=removed)
        d.cite(f"goal_rule:{rv.version}", rv.source, rv.effective_at)
        d.advice = Advice(False, "skipped", "mechanical repair of an existing page; no judgement needed")
        rows_raw = self.store.profile_rows(uid)
        events = self.store.events_for(uid, c["at"])
        verdict = self.verifier.verify(viewer_id=uid, trigger=pseudo_trigger, profile_rows=rows_raw, events=events,
                                       goal_id=plan["goal_id"], rule=rv, evidence=list(d.evidence), page=proposal,
                                       message=None, publish_at=at, deadline=parse_ts(plan["deadline"]), message_days=set(),
                                       tool_calls=0, model_calls=0, latency_s=0.0)
        # Eligibility was established when the plan was committed; a repair does not re-litigate it.
        blocking = [f for f in verdict.failed if not f.startswith(("goal_eligibility", "trigger_is_live_visit"))]
        d.checks_passed, d.checks_failed = verdict.passed, verdict.failed
        d.rationale.append(f"title {removed} withdrawn at {c['at']}; live page {page['action_id']} must not keep promoting it")
        if blocking or not (rows or hero):
            d.stop("NO_ACTION", "repair_failed", f"replacement page for {page['action_id']} not publishable "
                   f"({'; '.join(blocking) or 'no row keeps 3 titles'}); the platform has no unpublish call, flagged for review")
        else:
            d.page = proposal
            for tid in proposal.title_ids():
                d.cite(f"catalogue:{tid}", "data/catalogue.csv")
        res = self._finalise(d, clock, tools, 0.0)
        if res["actions"]:
            with self.store.tx() as conn:
                self.store.mark_superseded(conn, page["action_id"], res["actions"][0]["action_id"])
        return {"page": page["action_id"], "decision_id": d.decision_id, "status": d.status,
                "replacement": res["actions"][0]["action_id"] if res["actions"] else None}
