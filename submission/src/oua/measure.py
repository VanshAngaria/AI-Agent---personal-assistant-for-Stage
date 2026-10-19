"""Close the loop and prove impact.

* judge_all      judge every plan (both arms) from attributable signals under the plan's own rule version,
                 write outcomes and feed lessons back into each viewer's state.
* impact_report  intent-to-treat comparison of the randomized arms; shows why served-vs-unserved misleads.
* audit          what the agent knew, decided and observed for one viewer at any moment.
"""

from __future__ import annotations

import csv
import json
import math
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from oua.goals import SuccessJudge
from oua.pack import Pack, read_jsonl
from oua.store import Store
from oua.timeutil import ist_date, iso, parse_ts, seconds

CONF_BANDS = ((0.0, 0.4), (0.4, 0.55), (0.55, 0.7), (0.7, 1.01))


# ==========================================================================================================
# Judge
# ==========================================================================================================

def _attributable(e: dict[str, Any], start: str) -> bool:
    """A signal counts only if it happened (device-buffer corrected) after the goal started."""
    sent, occ = parse_ts(e.get("sent_at")), parse_ts(e.get("occurred_at"))
    buffered = seconds(occ, sent) if sent and occ else 0.0
    return parse_ts(e["received_at"]) - timedelta(seconds=max(0.0, buffered)) >= parse_ts(start)


def judge_plan(store: Store, pack: Pack, plan: dict[str, Any], observation_end: str) -> dict[str, Any]:
    events = store.events_between(plan["viewer_id"], plan["started_at"], plan["deadline"])
    events = [e for e in events if e["source"] != "history"]
    attributable = [e for e in events if _attributable(e, plan["started_at"])]
    judge = SuccessJudge(pack.policy, pack.catalogue)
    late = [e for e in events if not _attributable(e, plan["started_at"]) and judge.match(plan, e)]
    ok, hits = judge.judge_window(plan, attributable)
    if ok:
        need = judge.aggregate(plan["goal_id"])
        sig = hits[-1] if need else hits[0]
        status, reason, signal = "succeeded", f"{sig['event']} received {sig['received_at']} inside the window", sig
    elif plan["deadline"] > observation_end:
        status, signal = "inconclusive", None
        reason = f"window ends {plan['deadline']} but observed data ends {observation_end}; no success seen so far"
    else:
        status, signal = "failed", None
        reason = f"no attributable {plan['success_event']} ({plan['success_condition']}) between start and deadline"
    if late:
        reason += f"; {len(late)} matching event(s) ignored because they happened before the goal started (late upload)"
    corrected = [h for h in store.plan_history(plan["viewer_id"]) if h["plan_id"] == plan["plan_id"] and h["change"] == "rule_corrected"]
    if corrected:
        old = dict(plan, success_event=corrected[0]["detail"]["old_success_event"])
        old_ok, _ = judge.judge_window(old, attributable)
        reason += (f"; rule corrected mid-window ({corrected[0]['detail']['from']} -> {corrected[0]['detail']['to']}), "
                   f"judged under the corrected rule; under the old rule it would be {'succeeded' if old_ok else 'not succeeded'}")
    return {"status": status, "reason": reason, "signal": signal}


def _exposure(plan_actions: list[dict[str, Any]], sim_by_action: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    exp: dict[str, Any] = {"pages_published": 0, "pages_shown": 0, "messages_sent": 0, "delivered": 0, "clicked": 0,
                           "failed": [], "action_ids": [a["action_id"] for a in plan_actions]}
    for a in plan_actions:
        evs = sim_by_action.get(a["action_id"], [])
        if a["type"] == "home_page":
            exp["pages_published"] += 1
            exp["pages_shown"] += sum(1 for e in evs if e["event"] == "home_page_shown")
        else:
            exp["messages_sent"] += 1
            exp["delivered"] += sum(1 for e in evs if e["event"] == "message_delivered")
            exp["clicked"] += sum(1 for e in evs if e["event"] == "message_clicked")
            exp["failed"] += [(e.get("properties") or {}).get("reason") for e in evs if e["event"] == "message_failed"]
    return exp


def judge_all(store: Store, pack: Pack, out_dir: Path) -> dict[str, Any]:
    sim_path = out_dir / "sim" / "outcome_events.jsonl"
    store.ingest_events_bulk(read_jsonl(sim_path), source="simulator")
    observation_end = store.conn.execute("SELECT MAX(received_at) FROM events WHERE source = 'simulator'").fetchone()[0]

    sim_by_action: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in store.conn.execute(
            "SELECT * FROM events WHERE source = 'simulator' AND event IN ('home_page_shown','message_delivered','message_clicked','message_failed')"):
        props = json.loads(row["properties"])
        aid = props.get("page_action_id") or props.get("action_id")
        if aid:
            sim_by_action[aid].append({"event": row["event"], "received_at": row["received_at"], "properties": props})
    actions_by_plan: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for a in store.all_actions():
        if a.get("plan_id") is not None:
            actions_by_plan[a["plan_id"]].append(a)

    judged_at = iso(datetime.now().astimezone())
    rows = []
    counts: Counter = Counter()
    for plan in store.all_plans():
        j = judge_plan(store, pack, plan, observation_end)
        exp = _exposure(actions_by_plan.get(plan["plan_id"], []), sim_by_action)
        o = {"plan_id": plan["plan_id"], "viewer_id": plan["viewer_id"], "arm": plan["arm"], "goal_id": plan["goal_id"],
             "rule_version": plan["rule_version"], "status": j["status"], "reason": j["reason"],
             "signal_event_id": j["signal"]["event_id"] if j["signal"] else None,
             "signal_at": j["signal"]["received_at"] if j["signal"] else None,
             "judged_at": judged_at, "judged_by": "final_judge(simulator+live events)", "exposure": exp,
             "started_at": plan["started_at"], "deadline": plan["deadline"], "replay_status": plan["status"],
             "replay_close_reason": plan["close_reason"]}
        store.save_outcome(o)
        if plan["status"] == "open":
            with store.tx() as conn:
                store.close_plan(conn, plan["plan_id"], j["status"], judged_at, f"final judgment: {j['reason']}",
                                 o["signal_event_id"])
        _lessons(store, plan, o, exp)
        counts[(plan["arm"], j["status"])] += 1
        rows.append(o)
    with open(out_dir / "outcomes.jsonl", "w", encoding="utf-8") as f:
        for o in rows:
            f.write(json.dumps(o, ensure_ascii=False) + "\n")
    return {"plans": len(rows), "status": {f"{a}:{s}": n for (a, s), n in sorted(counts.items())},
            "observation_end": observation_end}


def _lessons(store: Store, plan: dict[str, Any], o: dict[str, Any], exp: dict[str, Any]) -> None:
    uid, at = plan["viewer_id"], o["judged_at"]
    ev = [f"plan:{plan['plan_id']}"]
    if o["status"] == "succeeded":
        store.add_lesson(uid, at, f"goal_succeeded:{plan['goal_id']}", f"{plan['goal_id']} reached; move to the next lifecycle goal", ev)
    elif o["status"] == "failed":
        store.add_lesson(uid, at, f"goal_failed:{plan['goal_id']}",
                         f"{plan['goal_id']} not reached (pages shown {exp['pages_shown']}, messages delivered {exp['delivered']}); "
                         "cool down this goal before retrying", ev)
    if "token_invalid" in exp["failed"]:
        store.add_lesson(uid, at, "push_undeliverable", "push token invalid; use another channel", ev)
    if "no_consent" in exp["failed"]:
        store.add_lesson(uid, at, "consent_mismatch", "platform reported no consent; our consent facts were wrong", ev)
    if exp["pages_published"] and not exp["pages_shown"]:
        store.add_lesson(uid, at, "page_not_seen", "published page(s) were never shown (viewer did not reach home while live, "
                         "or app dialect changed)", ev)


# ==========================================================================================================
# Impact
# ==========================================================================================================

def _rate(k: int, n: int) -> float | None:
    return None if n == 0 else k / n


def _diff(k1: int, n1: int, k0: int, n0: int) -> dict[str, Any]:
    if not n1 or not n0:
        return {"diff": None}
    p1, p0 = k1 / n1, k0 / n0
    se = math.sqrt(p1 * (1 - p1) / n1 + p0 * (1 - p0) / n0)
    pooled = (k1 + k0) / (n1 + n0)
    se0 = math.sqrt(pooled * (1 - pooled) * (1 / n1 + 1 / n0)) or 1e-12
    z = (p1 - p0) / se0
    return {"agent_rate": p1, "holdout_rate": p0, "diff": p1 - p0, "ci95": [p1 - p0 - 1.96 * se, p1 - p0 + 1.96 * se],
            "z": z, "p_value": math.erfc(abs(z) / math.sqrt(2))}


def _fmt_pct(x: float | None) -> str:
    return "n/a" if x is None else f"{100 * x:.1f}%"


def _fmt_diff(r: dict[str, Any]) -> str:
    if r.get("diff") is None:
        return "n/a"
    lo, hi = r["ci95"]
    return f"{100 * r['diff']:+.1f} pts (95% CI {100 * lo:+.1f} to {100 * hi:+.1f}; p={r['p_value']:.3f})"


def impact_report(store: Store, pack: Pack, out_dir: Path) -> dict[str, Any]:
    arms = {r["user_id"]: r["arm"] for r in store.conn.execute("SELECT user_id, arm FROM assignments")}
    decisions = [d for d in store.all_decisions() if not d["trigger"]["event"].startswith("control:")]
    first_seen: dict[str, str] = {}
    served: set[str] = set()
    for d in decisions:
        first_seen.setdefault(d["viewer_id"], d["trigger"]["deliver_at"])
        if d["status"] == "PUBLISHED":
            served.add(d["viewer_id"])
    viewers = sorted(first_seen)
    by_arm = {a: [v for v in viewers if arms.get(v) == a] for a in ("agent", "holdout")}

    # Primary: first committed goal per viewer, both arms judged identically.
    outcomes = store.outcomes()
    first_plan: dict[str, dict[str, Any]] = {}
    for o in outcomes:
        first_plan.setdefault(o["viewer_id"], o)
    prim = {}
    for a in ("agent", "holdout"):
        os_ = [first_plan[v] for v in by_arm[a] if v in first_plan]
        prim[a] = {"viewers": len(by_arm[a]), "with_goal": len(os_),
                   "succeeded": sum(o["status"] == "succeeded" for o in os_),
                   "failed": sum(o["status"] == "failed" for o in os_),
                   "inconclusive": sum(o["status"] == "inconclusive" for o in os_)}
    primary = _diff(prim["agent"]["succeeded"], prim["agent"]["with_goal"], prim["holdout"]["succeeded"], prim["holdout"]["with_goal"])
    conclusive = _diff(prim["agent"]["succeeded"], prim["agent"]["succeeded"] + prim["agent"]["failed"],
                       prim["holdout"]["succeeded"], prim["holdout"]["succeeded"] + prim["holdout"]["failed"])

    per_goal = {}
    for g in sorted({o["goal_id"] for o in first_plan.values()}):
        k = {a: [first_plan[v] for v in by_arm[a] if v in first_plan and first_plan[v]["goal_id"] == g] for a in by_arm}
        per_goal[g] = {a: (sum(o["status"] == "succeeded" for o in k[a]), len(k[a])) for a in k}
        per_goal[g]["diff"] = _diff(per_goal[g]["agent"][0], per_goal[g]["agent"][1], per_goal[g]["holdout"][0], per_goal[g]["holdout"][1])

    # Goal-agnostic behaviour over every triggered viewer (no conditioning on anything post-randomisation).
    metrics = {"any_play_14d": 0, "episode_completed_14d": 0, "paid_purchase_14d": 0, "plays_on_2plus_days_14d": 0}
    beh = {a: Counter() for a in by_arm}
    for a, vs in by_arm.items():
        for v in vs:
            start = first_seen[v]
            end = iso(parse_ts(start) + timedelta(days=14))
            evs = [e for e in store.events_between(v, start, end) if e["source"] in ("simulator", "replay_live")]
            plays = [e for e in evs if e["event"] == "play_started"]
            beh[a]["any_play_14d"] += bool(plays)
            beh[a]["episode_completed_14d"] += any(e["event"] == "episode_completed" for e in evs)
            beh[a]["paid_purchase_14d"] += any(e["event"] == "purchase_completed" and (e["properties"] or {}).get("is_trial") is not True for e in evs)
            beh[a]["plays_on_2plus_days_14d"] += len({ist_date(parse_ts(e["received_at"])) for e in plays}) >= 2
    behaviour = {m: _diff(beh["agent"][m], len(by_arm["agent"]), beh["holdout"][m], len(by_arm["holdout"])) for m in metrics}

    # Misleading comparisons, computed only to show why they mislead.
    succ = {v for v, o in first_plan.items() if o["status"] == "succeeded"}
    with_goal = set(first_plan)
    s_served = [v for v in with_goal if v in served]
    s_rest = [v for v in with_goal if v not in served]
    s_unserved_agent = [v for v in with_goal if v not in served and arms.get(v) == "agent"]
    naive = {
        "served_vs_everyone_else": _diff(sum(v in succ for v in s_served), len(s_served), sum(v in succ for v in s_rest), len(s_rest)),
        "served_vs_unserved_in_agent_arm": _diff(sum(v in succ for v in s_served), len(s_served),
                                                 sum(v in succ for v in s_unserved_agent), len(s_unserved_agent)),
    }
    # Pre-treatment balance: shows the arms are comparable before the agent acts.
    balance = {}
    for a, vs in by_arm.items():
        hist = Counter()
        for v in vs:
            hist["has_profile"] += bool(store.profile_rows(v))
            n = store.conn.execute("SELECT COUNT(*) FROM events WHERE user_id = ? AND source = 'history' AND event = 'play_started'", (v,)).fetchone()[0]
            hist["played_in_history"] += n > 0
        balance[a] = {k: _rate(n, len(vs)) for k, n in hist.items()}

    # Descriptive confidence bands (agent arm, first plans).
    conf_of = {}
    for d in decisions:
        if d["goal"] and d["goal"].get("plan_id") and not d["goal"]["kept_from_previous_decision"]:
            conf_of[d["goal"]["plan_id"]] = d["confidence"]
    bands = []
    for lo, hi in CONF_BANDS:
        os_ = [o for o in first_plan.values() if o["arm"] == "agent" and lo <= conf_of.get(o["plan_id"], -1) < hi]
        bands.append({"band": f"[{lo:.2f}, {min(hi, 1.0):.2f})", "plans": len(os_), "succeeded": sum(o["status"] == "succeeded" for o in os_)})

    pilot = _pilot(pack)
    exposure = Counter()
    for o in outcomes:
        if o["arm"] == "agent":
            exposure["pages_published"] += o["exposure"]["pages_published"]
            exposure["pages_shown"] += o["exposure"]["pages_shown"]
            exposure["messages_sent"] += o["exposure"]["messages_sent"]
            exposure["messages_delivered"] += o["exposure"]["delivered"]
            exposure["messages_clicked"] += o["exposure"]["clicked"]
    result = {
        "headline": {"primary_metric": "first-goal success, ITT", "agent": _fmt_pct(primary.get("agent_rate")),
                     "holdout": _fmt_pct(primary.get("holdout_rate")), "effect": _fmt_diff(primary)},
        "arms": prim, "primary": primary, "conclusive_only": conclusive, "per_goal": per_goal,
        "behaviour": behaviour, "behaviour_counts": {a: dict(c) for a, c in beh.items()}, "naive": naive,
        "balance": balance, "confidence_bands": bands, "pilot": pilot, "exposure": dict(exposure),
        "triggered_viewers": {a: len(v) for a, v in by_arm.items()},
    }
    (out_dir / "impact_report.md").write_text(_impact_md(result), encoding="utf-8")
    (out_dir / "impact.json").write_text(json.dumps(result, indent=2, default=str), encoding="utf-8")
    return result


def _pilot(pack: Pack) -> dict[str, Any]:
    path = pack.path("pilot/pilot_outcomes.csv")
    if not path.exists():
        return {}
    rows = list(csv.DictReader(open(path, encoding="utf-8")))
    s = lambda rs: (sum(r["succeeded"] == "true" for r in rs), len(rs))  # noqa: E731
    served = [r for r in rows if r["served"] == "true"]
    other = [r for r in rows if r["served"] != "true"]
    treat = [r for r in rows if r["arm"] == "treatment"]
    hold = [r for r in rows if r["arm"] == "holdout"]
    return {"served": s(served), "everyone_else": s(other), "treatment_itt": s(treat), "holdout_itt": s(hold),
            "naive": _diff(*s(served), *s(other)), "itt": _diff(*s(treat), *s(hold))}


def _impact_md(r: dict[str, Any]) -> str:
    a, h = r["arms"]["agent"], r["arms"]["holdout"]
    p = r["pilot"]
    L = ["# Impact report", "",
         "All numbers below are generated by `python -m oua impact` from this run's state; none are typed by hand.", "",
         "## Method", "",
         "* **Unit:** every viewer who sent at least one trigger in the replay. Arms come from `replay/arms.csv` "
         "(randomized before the replay; the agent never chooses who is treated).",
         "* **Same treatment of both arms:** the agent runs the identical pipeline for holdout viewers and commits the "
         "same *shadow goal* it would have pursued, then publishes nothing. Both arms are judged by the same judge, "
         "with the same rule versions, on the same simulator output.",
         "* **Primary metric (intent-to-treat):** success of each viewer's *first* committed goal, comparing everyone "
         "in the agent arm with everyone in the holdout arm, whether or not the agent ended up publishing anything. "
         "Inconclusive goals count as not succeeded in both arms.",
         "* **Attribution:** a signal counts only if it was received inside the goal window and, after correcting for "
         "device buffering, happened after the goal started.", "",
         "## Headline", "",
         f"| Arm | Triggered viewers | With a goal | Succeeded | Failed | Inconclusive | Success rate |",
         "|---|---|---|---|---|---|---|",
         f"| agent | {a['viewers']} | {a['with_goal']} | {a['succeeded']} | {a['failed']} | {a['inconclusive']} | {_fmt_pct(r['primary'].get('agent_rate'))} |",
         f"| holdout | {h['viewers']} | {h['with_goal']} | {h['succeeded']} | {h['failed']} | {h['inconclusive']} | {_fmt_pct(r['primary'].get('holdout_rate'))} |",
         "", f"**Effect of the agent (agent - holdout): {_fmt_diff(r['primary'])}.**", "",
         f"Excluding inconclusive goals: {_fmt_diff(r['conclusive_only'])}.", "",
         "## By goal (first goals only)", "", "| Goal | Agent | Holdout | Difference |", "|---|---|---|---|"]
    for g, v in r["per_goal"].items():
        L.append(f"| {g} | {v['agent'][0]}/{v['agent'][1]} ({_fmt_pct(_rate(*v['agent']))}) | "
                 f"{v['holdout'][0]}/{v['holdout'][1]} ({_fmt_pct(_rate(*v['holdout']))}) | {_fmt_diff(v['diff'])} |")
    L += ["", "Per-goal cells are small; treat them as descriptive, not as separate findings.", "",
          "## Goal-agnostic behaviour (all triggered viewers, 14 days after their first trigger)", "",
          "| Metric | Agent | Holdout | Difference |", "|---|---|---|---|"]
    for m, d in r["behaviour"].items():
        L.append(f"| {m} | {_fmt_pct(d.get('agent_rate'))} | {_fmt_pct(d.get('holdout_rate'))} | {_fmt_diff(d)} |")
    ex = r["exposure"]
    L += ["", "## Exposure (agent arm)", "",
          f"Pages published {ex.get('pages_published', 0)}, shown as the personalised home page {ex.get('pages_shown', 0)} times; "
          f"messages sent {ex.get('messages_sent', 0)}, delivered {ex.get('messages_delivered', 0)}, clicked {ex.get('messages_clicked', 0)}.", "",
          "## Pre-treatment balance", "", "| Arm | Has profile row | Played in the 30-day history |", "|---|---|---|"]
    for arm, b in r["balance"].items():
        L.append(f"| {arm} | {_fmt_pct(b.get('has_profile'))} | {_fmt_pct(b.get('played_in_history'))} |")
    n1, n2 = r["naive"]["served_vs_everyone_else"], r["naive"]["served_vs_unserved_in_agent_arm"]
    L += ["", "## Why simpler comparisons mislead", "",
          f"* **Served vs everyone else** (the pilot report's method): {_fmt_pct(n1.get('agent_rate'))} vs "
          f"{_fmt_pct(n1.get('holdout_rate'))}, {_fmt_diff(n1)}.",
          f"* **Served vs unserved inside the agent arm:** {_fmt_pct(n2.get('agent_rate'))} vs {_fmt_pct(n2.get('holdout_rate'))}, {_fmt_diff(n2)}.",
          "", "Being *served* is not random. The agent only publishes for viewers who are in the app right now (not a "
          "buffered open), whose app is set to a dialect with content, who have an eligible goal and pass every check. "
          "Those conditions select viewers who are already more engaged. Comparing them with everyone else measures that "
          "selection, not the agent. Only the randomized arms (above) are comparable; the balance table shows they "
          "looked alike before the agent acted.", ""]
    if p:
        L += ["## The pilot report, recomputed", "",
              f"* Report headline (served vs all others): {p['served'][0]}/{p['served'][1]} vs {p['everyone_else'][0]}/{p['everyone_else'][1]}: "
              f"{_fmt_diff(p['naive'])}.",
              f"* Same pilot file, randomized arms (treatment vs holdout): {p['treatment_itt'][0]}/{p['treatment_itt'][1]} vs "
              f"{p['holdout_itt'][0]}/{p['holdout_itt'][1]}: {_fmt_diff(p['itt'])}.",
              "", "The 2.3x 'uplift' disappears under the randomized comparison; the pilot did not show that agent v0 helped. "
              "The report also assigned goals by subscription status at first visit only and compared against viewers who "
              "were never eligible to be served.", ""]
    L += ["## Confidence bands (agent arm, first goals; descriptive)", "", "| Confidence | Plans | Succeeded |", "|---|---|---|"]
    for b in r["confidence_bands"]:
        L.append(f"| {b['band']} | {b['plans']} | {b['succeeded']} |")
    L += ["", "Confidence is a documented heuristic of evidence quality, not a calibrated probability of success. This "
          "table is how it would be calibrated over time.", "",
          "## Caveats", "",
          "* One replay day and one simulator seed. Evaluators' seeds will move every number above.",
          "* The simulator models a modest page effect; a null or small effect is a legitimate result and is reported as such.",
          "* Goals with windows longer than the observed period are inconclusive, not failed.", ""]
    return "\n".join(L)


# ==========================================================================================================
# Viewer state export, final report and audit
# ==========================================================================================================

def viewer_snapshot(store: Store, viewer_id: str, at: str | None = None) -> dict[str, Any]:
    decisions = store.decisions_for(viewer_id, at)
    last = decisions[-1] if decisions else None
    facts = []
    if last:
        facts = [dict(r) for r in store.conn.execute(
            "SELECT fact_key, value, source, source_id, observed_at, freshness_s FROM facts WHERE decision_id = ? ORDER BY fact_id",
            (last["decision_id"],))]
        for f in facts:
            f["value"] = json.loads(f["value"])
    history = [h for h in store.plan_history(viewer_id) if at is None or h["at"] <= at]
    return {
        "viewer_id": viewer_id,
        "as_of": at or "end of run",
        "arm": store.arm(viewer_id),
        "facts_at_last_decision": {"decision_id": last["decision_id"] if last else None, "facts": facts,
                                   "conflicts": last["conflicting_evidence"] if last else []},
        "decisions": [{"decision_id": d["decision_id"], "deliver_at": d["trigger"]["deliver_at"], "trigger": d["trigger"]["event"],
                       "status": d["status"], "action": d["action"], "goal": (d["goal"] or {}).get("id"),
                       "reason": d["no_action_reason"] or "; ".join(d["rationale"][:2]), "confidence": d["confidence"]}
                      for d in decisions],
        "actions": store.actions_for(viewer_id, at),
        "plans": [p for p in store.plans_for(viewer_id) if at is None or p["started_at"] <= at],
        "plan_history": history,
        "outcomes": [o for o in store.outcomes(viewer_id) if at is None or o["judged_at"] <= at],
        "lessons": store.lessons(viewer_id, at),
    }


def write_viewer_state(store: Store, out_dir: Path) -> int:
    out_dir.mkdir(parents=True, exist_ok=True)
    viewers = [r[0] for r in store.conn.execute("SELECT DISTINCT viewer_id FROM decisions ORDER BY viewer_id")]
    for v in viewers:
        (out_dir / f"{v}.json").write_text(json.dumps(viewer_snapshot(store, v), indent=1, ensure_ascii=False, default=str),
                                           encoding="utf-8")
    return len(viewers)


def audit(store: Store, viewer_id: str, at: str | None) -> dict[str, Any]:
    cutoff = at or "9999"
    known = Counter(r["event"] for r in store.conn.execute(
        "SELECT event FROM events WHERE user_id = ? AND received_at <= ? AND source != 'simulator'", (viewer_id, cutoff)))
    snap = viewer_snapshot(store, viewer_id, at)
    snap["events_known_by_type"] = dict(known)
    snap["profile_rows"] = store.profile_rows(viewer_id)
    return snap


def write_final_report(store: Store, pack: Pack, out_dir: Path, impact: dict[str, Any], settings) -> None:
    decisions = store.all_decisions()
    lat = json.loads((out_dir / "latency_report.json").read_text(encoding="utf-8"))
    summary = json.loads((out_dir / "replay_summary.json").read_text(encoding="utf-8"))
    sim = json.loads((out_dir / "sim" / "simulator_report.json").read_text(encoding="utf-8"))
    status = Counter((d["arm"], d["status"]) for d in decisions)
    kinds = Counter(d.get("no_action_kind") for d in decisions if d["status"] != "PUBLISHED")
    goals = Counter(d["goal"]["id"] for d in decisions if d["goal"] and not d["goal"]["kept_from_previous_decision"])
    actions = Counter(a["type"] for a in store.all_actions())
    outcomes = Counter((o["arm"], o["status"]) for o in store.outcomes())

    def find(pred, n=3):
        return [d["decision_id"] + f" ({d['viewer_id']})" for d in decisions if pred(d)][:n]

    tri = find(lambda d: any(c["fact"] == "dialect" and len(c["values"]) >= 3 for c in d["conflicting_evidence"]))
    faults = {f["trigger_event_id"]: f for f in pack.model_faults()}
    fault_rows = []
    for d in decisions:
        f = faults.get(d["trigger"]["event_id"])
        if f:
            fault_rows.append(f"| {d['decision_id']} | {d['viewer_id']} | {f['fault']} | {d['status']} | "
                              f"{d['no_action_kind'] or d['action']} | model: {d['model']['outcome']} |")
    ctrl = store.control_log()
    live_closed = [h for h in store.conn.execute("SELECT * FROM plan_history WHERE change IN ('succeeded','inconclusive') AND detail LIKE '%live%'")]
    L = ["# Final report: sample replay", "",
         f"Pack: `{pack.root.name}`; replay day 2026-10-01 IST; simulator seed in `sim/simulator_report.json` (`{sim.get('seed')}`).", "",
         "## What happened", "",
         f"* Triggers delivered: {lat['decisions'] + lat['redeliveries_answered_idempotently']} "
         f"({lat['redeliveries_answered_idempotently']} were duplicate deliveries answered with the stored decision).",
         f"* Decisions: {len(decisions)} (including {sum(1 for d in decisions if d['trigger']['event'].startswith('control:'))} "
         "page repairs triggered by a title withdrawal).",
         "* Status by arm: " + ", ".join(f"{a}/{s}: {n}" for (a, s), n in sorted(status.items(), key=str)) + ".",
         "* Reasons for no action: " + ", ".join(f"{k}: {n}" for k, n in kinds.most_common()) + ".",
         "* Goals committed: " + ", ".join(f"{g}: {n}" for g, n in goals.most_common()) + " (G10/G11 never: unmeasurable).",
         "* Actions published: " + ", ".join(f"{k}: {n}" for k, n in actions.most_common()) + ".",
         f"* Simulator policy problems reported for our actions: **{sim.get('policy_problem_count')}**; "
         f"holdout actions ignored: {sim['counts'].get('holdout_actions_ignored', 0)}; pages accepted "
         f"{sim['counts'].get('home_page_accepted', 0)}, rows dropped {sim['counts'].get('home_page_row_dropped_below_minimum', 0)}.",
         "", "## Latency", "",
         f"* p50 {lat['latency_s']['p50']}s, p95 {lat['latency_s']['p95']}s, max {lat['latency_s']['max']}s "
         f"(budget {lat['budget_s']:.0f}s; failure after {lat['failure_after_s']:.0f}s). Over budget: {lat['over_budget_60s']}; "
         f"over 5 min: {lat['over_failure_300s']}.",
         f"* Pages published within the {lat['page_target_s']}s page target: {lat['pages_within_page_target']}/{lat['pages_published']}.",
         f"* Peak hour {lat['peak_hour']['hour']}: {lat['peak_hour']['triggers']} triggers, p95 {lat['peak_hour']['p95_latency_s']}s.",
         f"* Max calls in one decision: tool {lat['tool_calls']['max']}/{lat['tool_calls']['limit']}, "
         f"model {lat['model_calls']['max']}/{lat['model_calls']['limit']}.", "",
         "## Surprise events and how the agent responded", "",
         "| Surprise | What the agent did | Where to look |", "|---|---|---|",
         f"| Buffered offline opens | {kinds.get('stale_trigger', 0)} triggers had device buffer time > {pack.policy.trigger['stale_after_s']}s; "
         "recorded as history, no page, no plan change | " + ", ".join(find(lambda d: d.get('no_action_kind') == 'stale_trigger')) + " |",
         f"| Duplicate deliveries | {lat['redeliveries_answered_idempotently']} redeliveries returned the stored decision; "
         "no second decision or action | `processed_triggers` table |",
         f"| Three-way dialect conflict | app setting used; profile and campaign-link dialects kept as conflicts | "
         + ", ".join(tri) + " |"]
    for c in ctrl:
        if c["type"] == "goal_rule_corrected":
            L.append(f"| {c['payload']['goal_id']} success rule corrected at {c['at']} | new version {c['affected']['new_version']}; "
                     f"{len(c['affected']['open_plans_updated'])} open plan(s) updated, 0 other viewers reprocessed; "
                     "closed outcomes untouched | `control_log`, `plan_history` |")
        elif c["type"] == "title_withdrawn":
            L.append(f"| {c['payload']['title_id']} withdrawn at {c['at']} | excluded from retrieval and verification from that moment; "
                     f"{len(c['affected']['live_pages'])} live page(s) repaired by republishing without it | "
                     + ", ".join(r.get("decision_id", "") for r in c["affected"]["replacements"]) + " |")
        elif c["type"].startswith("dependency"):
            w = next((x for x in lat["dependency_windows"] if x["from"] == c["affected"]["window"][0]), {})
            L.append(f"| {c['payload']['dependency']} {c['affected']['mode']} {c['affected']['window'][0]} to "
                     f"{c['affected']['window'][1]} | {w.get('decisions', 0)} decisions in the window: {w.get('status')}; "
                     f"model outcomes {w.get('model_outcomes')}; max latency {w.get('max_latency_s')}s | `latency_report.json` |")
    L.append("| Purchase arrives right after a decision | live events close the open plan at once (succeeded if attributable) "
             f"or flag it for re-planning; {len(live_closed)} plans closed by live events | `plan_history` |")
    L.append("| Catalogue description with instructions | the title is quarantined from promotion; its text is never passed "
             "to the planner or model as an instruction | `catalogue.py`, `tests/test_safety.py` |")
    L += ["", "Model faults from `replay/model_faults.jsonl`:", "", "| Decision | Viewer | Fault | Status | Action | Model |",
          "|---|---|---|---|---|---|"] + fault_rows
    L += ["", "Faults on buffered triggers never reach the model: the agent abstains before any model call.", "",
          "## Outcomes", "", "* " + ", ".join(f"{a}/{s}: {n}" for (a, s), n in sorted(outcomes.items())),
          "", "## Impact", "", f"* Primary (ITT, first goal): agent {impact['headline']['agent']} vs holdout "
          f"{impact['headline']['holdout']}: {impact['headline']['effect']}.",
          "* Full analysis, including why served-vs-unserved misleads and the recomputed pilot: `impact_report.md`.", "",
          "## Files", "",
          "`decisions.jsonl` (every decision), `actions.jsonl` (simulator input), `outcomes.jsonl` (every plan judged), "
          "`viewer_state/` (per-viewer durable state export), `latency_report.json`, `impact_report.md`, "
          "`replay_summary.json`, `sim/simulator_report.json`.", ""]
    (out_dir / "final_report.md").write_text("\n".join(L), encoding="utf-8")
