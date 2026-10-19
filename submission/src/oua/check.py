"""Evaluation check: verifies the submission against the brief (sections 8 and 9, and "what will score poorly").

    python -m oua check               # checks the files and the sample run
    python -m oua check --run-tests   # also runs the test suite

Every check reads generated artefacts (sample_run/, the decision records, the simulator's own audit) and, when the
data pack is present, the pack itself, so nothing here restates a claim from the documentation. Writes
sample_run/evaluation_check.md and exits non-zero if any check fails.
"""

from __future__ import annotations

import csv
import json
import re
import subprocess
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from oua.timeutil import in_quiet_hours, ist_date, parse_ts

REQUIRED_LAYOUT = ["README.md", "ARCHITECTURE.md", "AI_COLLABORATION.md", "KNOWN_LIMITATIONS.md", "src", "tests",
                   "sample_run/decisions.jsonl", "sample_run/viewer_state", "sample_run/actions.jsonl",
                   "sample_run/outcomes.jsonl", "sample_run/latency_report.json", "sample_run/impact_report.md",
                   "sample_run/final_report.md"]
ARCH_TOPICS = {"ownership": r"##\s*Ownership", "planning": r"##\s*Planning", "memory": r"##\s*Memory",
               "verification": r"##\s*Verification", "latency": r"##\s*Latency", "failure recovery": r"##\s*Failure recovery",
               "trust boundaries": r"##\s*Trust boundaries"}
REQUIRED_TESTS = {"late or duplicate trigger": ("test_idempotency.py", "duplicate"),
                  "conflicting source": ("test_conflicts.py", "conflict"),
                  "tool failure": ("test_failures.py", "outage"),
                  "corrected goal rule": ("test_goal_correction.py", "correction")}


@dataclass
class Check:
    section: str
    name: str
    status: str  # PASS | FAIL | SKIP | INFO
    evidence: str


class Checker:
    def __init__(self, sub: Path, pack: Path | None) -> None:
        self.sub = sub
        self.run = sub / "sample_run"
        self.pack = pack if pack and (pack / "data" / "catalogue.csv").exists() else None
        self.out: list[Check] = []
        self.decisions = self._jsonl("decisions.jsonl")
        self.actions = self._jsonl("actions.jsonl")
        self.outcomes = self._jsonl("outcomes.jsonl")
        self.latency = self._json("latency_report.json")
        self.impact = self._json("impact.json")
        self.sim = self._json("sim/simulator_report.json")
        self.summary = self._json("replay_summary.json")
        self.real = [d for d in self.decisions if not d["trigger"]["event"].startswith("control:")]
        self.catalogue = self._catalogue()

    # -- helpers ---------------------------------------------------------------------------------------------------

    def _jsonl(self, name: str) -> list[dict[str, Any]]:
        p = self.run / name
        return [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines() if x.strip()] if p.exists() else []

    def _json(self, name: str) -> dict[str, Any]:
        p = self.run / name
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}

    def _catalogue(self) -> dict[str, dict[str, str]]:
        if not self.pack:
            return {}
        with open(self.pack / "data" / "catalogue.csv", encoding="utf-8") as f:
            return {r["title_id"]: r for r in csv.DictReader(f)}

    def add(self, section: str, name: str, ok: bool | None, evidence: str) -> None:
        status = "SKIP" if ok is None else ("PASS" if ok else "FAIL")
        self.out.append(Check(section, name, status, evidence))

    def info(self, section: str, name: str, evidence: str) -> None:
        self.out.append(Check(section, name, "INFO", evidence))

    def text(self, rel: str) -> str:
        p = self.sub / rel
        return p.read_text(encoding="utf-8") if p.exists() else ""

    # -- section 8: submission requirements ------------------------------------------------------------------------

    def submission(self) -> None:
        s = "8 Submission"
        missing = [p for p in REQUIRED_LAYOUT if not (self.sub / p).exists()]
        self.add(s, "Required layout (submission/ tree from the brief)", not missing,
                 "all present" if not missing else f"missing: {missing}")
        readme, cli = self.text("README.md"), self.text("src/oua/cli.py")
        self.add(s, "Runnable CLI with setup and one replay command",
                 "python -m oua run-all" in readme and "pip install" in readme and '"run-all"' in cli,
                 "README documents `pip install -e .` and `python -m oua run-all`; cli.py defines run-all")
        viewers = {d["viewer_id"] for d in self.decisions}
        vs_dir = self.run / "viewer_state"
        files = list(vs_dir.glob("*.json")) if vs_dir.exists() else []
        sample = json.loads(files[0].read_text(encoding="utf-8")) if files else {}
        keys = {"facts_at_last_decision", "decisions", "actions", "plans", "plan_history", "outcomes", "lessons"}
        self.add(s, "Durable per-viewer state", len(files) == len(viewers) and keys <= set(sample),
                 f"{len(files)} viewer files for {len(viewers)} viewers; each has {sorted(keys & set(sample))}")
        try:
            from oua.schemas import Decision
            bad = 0
            for d in self.decisions:
                try:
                    Decision.model_validate(d)
                except Exception:
                    bad += 1
            self.add(s, "Evidence references and decision outputs", bool(self.decisions) and bad == 0,
                     f"{len(self.decisions)} decisions validate against the schema (every cited evidence id resolves); {bad} invalid")
        except ImportError as exc:
            self.add(s, "Evidence references and decision outputs", False, f"schema import failed: {exc}")
        settings, llm = self.text("src/oua/settings.py"), self.text("src/oua/llm.py")
        self.add(s, "Provider abstraction / mock mode (no personal API key)",
                 'llm_provider: str = "mock"' in settings and "class MockProvider" in llm and "class ModelProvider" in llm,
                 "default provider is the local mock; ModelProvider protocol with Mock and Anthropic implementations")
        tests_dir = self.sub / "tests"
        for need, (fname, _) in REQUIRED_TESTS.items():
            p = tests_dir / fname
            n = len(re.findall(r"^def test_", p.read_text(encoding="utf-8"), re.M)) if p.exists() else 0
            self.add(s, f"Automated test: {need}", n > 0, f"{fname}: {n} test functions")
        no_action = [d for d in self.decisions if d["status"] != "PUBLISHED"]
        no_reason = [d for d in no_action if not (d.get("no_action_reason") or "").strip()]
        self.add(s, "Sample replay: decisions, actions, reasons for no action, outcomes, impact report",
                 bool(self.actions) and bool(self.outcomes) and not no_reason and (self.run / "impact_report.md").exists(),
                 f"{len(self.decisions)} decisions, {len(self.actions)} actions, {len(no_action)} no-action decisions all with a reason, "
                 f"{len(self.outcomes)} outcome judgments, impact report present")
        arch = self.text("ARCHITECTURE.md")
        missing_topics = [t for t, rx in ARCH_TOPICS.items() if not re.search(rx, arch, re.I)]
        self.add(s, "Architecture note covers the 7 required topics", not missing_topics,
                 "sections: " + ", ".join(ARCH_TOPICS) if not missing_topics else f"missing: {missing_topics}")
        ai = self.text("AI_COLLABORATION.md").lower()
        self.add(s, "AI collaboration log (how tools helped, how output was checked)",
                 "checked" in ai and "rejected" in ai, "describes checks (replay inspection, mutation testing) and rejected suggestions")
        self.add(s, "Known-limitations note", len(self.text("KNOWN_LIMITATIONS.md")) > 500, "KNOWN_LIMITATIONS.md present and substantive")

    # -- section 9: evaluation areas ---------------------------------------------------------------------------------

    def goal_quality(self) -> None:
        s = "9 Goal and decision quality (20%)"
        committed = Counter(d["goal"]["id"] for d in self.decisions if d.get("goal") and not d["goal"].get("kept_from_previous_decision"))
        self.add(s, "Unmeasurable goals (G10, G11) never committed", not (committed.get("G10") or committed.get("G11")),
                 "goals committed: " + ", ".join(f"{g} {n}" for g, n in committed.most_common()))
        pub = [d for d in self.decisions if d["status"] == "PUBLISHED"]
        self.add(s, "Every published decision has a committed goal", all(d.get("goal") for d in pub), f"{len(pub)} published decisions")
        per_decision = [Counter(a["type"] for a in d["actions"]) for d in pub]
        prop = all(c.get("home_page", 0) <= 1 and c.get("push", 0) + c.get("whatsapp", 0) <= 1 for c in per_decision)
        self.add(s, "Proportionate: at most one page and one message per decision", prop,
                 f"max actions in one decision: {max((sum(c.values()) for c in per_decision), default=0)}")
        kinds = Counter(d.get("no_action_kind") for d in self.decisions if d["status"] != "PUBLISHED")
        self.add(s, "Doing nothing is a recorded, reasoned decision", bool(kinds), ", ".join(f"{k} {n}" for k, n in kinds.most_common()))
        shadow = sum(1 for d in self.decisions if d["arm"] == "holdout" and d.get("goal"))
        self.add(s, "Holdout gets shadow goals (same goal logic, no action)", shadow > 0, f"{shadow} holdout decisions record a shadow goal")

    def evidence_state(self) -> None:
        s = "9 Evidence and state (15%)"
        facts = [e for d in self.decisions for e in d["evidence_details"] if e["id"].startswith("fact:")]
        with_src = sum(1 for e in facts if e.get("source"))
        timed = sum(1 for e in facts if e.get("observed_at") or e.get("source") == "none")
        self.add(s, "Facts carry source and time/freshness", facts and with_src == len(facts) and timed == len(facts),
                 f"{len(facts)} cited facts; {with_src} with source; {timed} with an observation time (or explicitly unknown)")
        conflicts = sum(1 for d in self.decisions if d["conflicting_evidence"])
        triple = sum(1 for d in self.decisions if any(c["fact"] == "dialect" and len(c["values"]) >= 3 for c in d["conflicting_evidence"]))
        self.add(s, "Conflicts stay visible, with the chosen source and why", conflicts > 0 and all(
            c.get("why") for d in self.decisions for c in d["conflicting_evidence"]),
                 f"{conflicts} decisions carry conflicts ({triple} with three dialect values), each with a reason")
        open_multi = 0
        for f in (self.run / "viewer_state").glob("*.json"):
            st = json.loads(f.read_text(encoding="utf-8"))
            if sum(1 for p in st["plans"] if p["status"] == "open") > 1:
                open_multi += 1
        self.add(s, "State is consistent: at most one open plan per viewer", open_multi == 0,
                 f"{open_multi} viewers with more than one open plan (also a database constraint)")

    def realtime(self) -> None:
        s = "9 Real-time reliability (15%)"
        lat = self.latency.get("latency_s", {})
        self.add(s, "Every decision within the 60 s budget; none past 5 min",
                 self.latency.get("over_budget_60s") == 0 and self.latency.get("over_failure_300s") == 0,
                 f"p50 {lat.get('p50')} s, p95 {lat.get('p95')} s, max {lat.get('max')} s")
        self.info(s, "Pages within the 3 s page target",
                  f"{self.latency.get('pages_within_page_target')}/{self.latency.get('pages_published')}")
        trig_ids = [d["trigger"]["event_id"] for d in self.decisions]
        redel = self.latency.get("redeliveries_answered_idempotently", 0)
        self.add(s, "Triggers are idempotent (one decision per trigger, duplicates answered from state)",
                 len(trig_ids) == len(set(trig_ids)) and redel > 0, f"{len(trig_ids)} decisions, all unique triggers; {redel} redeliveries answered")
        stale = [d for d in self.decisions if d.get("no_action_kind") == "stale_trigger"]
        self.add(s, "Late (buffered) triggers handled: recorded, no action", bool(stale) and all(not d["actions"] for d in stale),
                 f"{len(stale)} buffered opens recorded with no action")
        win = self.latency.get("dependency_windows", [])
        ok = all((w.get("max_latency_s") or 0) <= 60 for w in win) and all(
            not d["actions"] for d in self.decisions if d["status"] == "FAILED_SAFE")
        self.add(s, "Failures degrade safely inside outage windows", bool(win) and ok,
                 "; ".join(f"{w['dependency']} {w['mode']}: {w['status']}, max {w['max_latency_s']} s" for w in win))

    def safety(self) -> None:
        s = "9 Verification and safety (15%)"
        arm_of = {d["viewer_id"]: d["arm"] for d in self.decisions}
        hold = [a for a in self.actions if arm_of.get(a["user_id"]) != "agent"]
        self.add(s, "No action of any kind for holdout viewers", not hold and self.sim.get("counts", {}).get("holdout_actions_ignored", 0) == 0,
                 f"{len(hold)} holdout actions; simulator ignored {self.sim.get('counts', {}).get('holdout_actions_ignored', 0)}")
        if self.catalogue:
            dec_dialect = {d["decision_id"]: d["dialect"]["used"] for d in self.decisions}
            wrong = 0
            for a in self.actions:
                ids = ([a["hero_title_id"]] if a.get("hero_title_id") else []) + [t for r in a.get("rows", []) for t in r["title_ids"]]
                ids += [a["title_id"]] if a.get("title_id") else []
                want = a.get("dialect") or dec_dialect.get(a["decision_id"])
                wrong += sum(1 for t in ids if self.catalogue.get(t, {}).get("dialect") != want)
                if a["type"] == "home_page" and a.get("dialect") != dec_dialect.get(a["decision_id"]):
                    wrong += 1
            self.add(s, "No wrong-dialect content (every title in the dialect the app shows)", wrong == 0,
                     f"{wrong} wrong-dialect titles across {len(self.actions)} actions (checked against data/catalogue.csv)")
        else:
            self.add(s, "No wrong-dialect content", None, "data pack not present")
        msgs = [a for a in self.actions if a["type"] in ("push", "whatsapp")]
        night = [a for a in msgs if in_quiet_hours(parse_ts(a["send_at"]))]
        per_day = Counter((a["user_id"], ist_date(parse_ts(a["send_at"]))) for a in msgs)
        self.add(s, "No messages in quiet hours (22:00-08:00 IST)", not night, f"{len(night)} of {len(msgs)} messages in quiet hours")
        self.add(s, "Frequency cap: at most one push/WhatsApp per viewer per IST day", max(per_day.values(), default=0) <= 1,
                 f"max per viewer-day: {max(per_day.values(), default=0)}")
        c = self.sim.get("counts", {})
        self.add(s, "Consent respected (simulator's independent audit)", c.get("push_blocked_no_consent", 0) == 0
                 and c.get("whatsapp_blocked_no_consent", 0) == 0 and self.sim.get("policy_problem_count") == 0,
                 f"blocked for no consent: push {c.get('push_blocked_no_consent', 0)}, WhatsApp {c.get('whatsapp_blocked_no_consent', 0)}; "
                 f"simulator policy problems: {self.sim.get('policy_problem_count')}")
        if self.pack:
            from oua.catalogue import Catalogue
            from oua.policy import Policy
            cat = Catalogue.load(self.pack / "data" / "catalogue.csv", self.pack / "data" / "episodes.csv")
            flagged = set(cat.screen(Policy.load().safety["instruction_patterns"]))
            promoted = {t for a in self.actions for t in ([a.get("hero_title_id"), a.get("title_id")] +
                                                          [x for r in a.get("rows", []) for x in r["title_ids"]]) if t}
            wa_blast = sum(1 for a in self.actions if a["type"] == "whatsapp")
            self.add(s, "Injected catalogue instructions never acted on", not (flagged & promoted),
                     f"flagged titles {sorted(flagged)}; promoted: {sorted(flagged & promoted) or 'none'}; WhatsApp actions in total: {wa_blast}")
            faults = [json.loads(x) for x in (self.pack / "replay" / "model_faults.jsonl").read_text(encoding="utf-8").splitlines() if x.strip()]
            by_trig = {d["trigger"]["event_id"]: d for d in self.decisions}
            claimed_ok = all((by_trig.get(f["trigger_event_id"], {}).get("goal") or {}).get("id") != f["model_output"].get("goal_id")
                             for f in faults if f["fault"] == "unsupported_claim")
            self.add(s, "Ineligible goals caught: unsupported model claims rejected", claimed_ok,
                     "no decision adopted a goal the model claimed without evidence; outcomes: " + ", ".join(
                         f"{by_trig[f['trigger_event_id']]['viewer_id']} {by_trig[f['trigger_event_id']]['model']['outcome']}"
                         for f in faults if f["trigger_event_id"] in by_trig))
        templates_ok = all(a.get("template_id") == "wa_recommend_title_v1" for a in self.actions if a["type"] == "whatsapp")
        self.add(s, "Only approved WhatsApp templates", templates_ok, "all WhatsApp actions use an approved template")

    def measurement(self) -> None:
        s = "9 Measurement and learning (15%)"
        arms = Counter(o["arm"] for o in self.outcomes)
        statuses = Counter(o["status"] for o in self.outcomes)
        self.add(s, "Outcomes judged for both arms from attributable signals", arms.get("agent", 0) > 0 and arms.get("holdout", 0) > 0,
                 f"{dict(arms)}; {dict(statuses)}")
        head = self.impact.get("headline", {})
        self.add(s, "Impact measured against the randomized holdout (ITT)", "ITT" in head.get("primary_metric", ""),
                 f"{head.get('primary_metric')}: agent {head.get('agent')} vs holdout {head.get('holdout')}, {head.get('effect')}")
        report = self.text("sample_run/impact_report.md").lower()
        self.add(s, "Explains why simpler comparisons mislead", "why simpler comparisons mislead" in report and "pilot" in report,
                 "impact report shows served-vs-unserved and the pilot headline only as warnings, recomputed against randomized arms")
        lessons = sum(1 for f in (self.run / "viewer_state").glob("*.json") if json.loads(f.read_text(encoding="utf-8"))["lessons"])
        self.add(s, "Results feed back into the viewer's state (lessons)", lessons > 0, f"{lessons} viewers have recorded lessons")

    def engineering(self, run_tests: bool) -> None:
        s = "9 Engineering quality (10%)"
        self.add(s, "Package, one command, pinned minimal dependencies", (self.sub / "pyproject.toml").exists(),
                 "pyproject.toml with extras [dev], [llm]; runtime dependency: pydantic only")
        if run_tests:
            r = subprocess.run([sys.executable, "-m", "pytest", "-p", "no:cacheprovider"], cwd=self.sub,
                               capture_output=True, text=True)
            tail = (r.stdout.strip().splitlines() or ["no output"])[-1].strip("= ")
            self.add(s, "Test suite passes", r.returncode == 0, tail)
        else:
            n = sum(len(re.findall(r"^def test_", p.read_text(encoding="utf-8"), re.M)) for p in (self.sub / "tests").glob("test_*.py"))
            self.add(s, "Test suite present (run with --run-tests to execute)", n > 0, f"{n} test functions (75 cases with parametrisation)")
        self.add(s, "Reproducible", (self.run / "sim" / "simulator_report.json").exists(),
                 f"deterministic replay; simulator seed `{self.sim.get('seed')}`; outputs regenerated by `python -m oua run-all`")

    def ai_tools(self) -> None:
        s = "9 Use of AI tools (10%)"
        ai = self.text("AI_COLLABORATION.md").lower()
        self.add(s, "Directs, checks and sometimes rejects AI output",
                 "mutation" in ai and "rejected" in ai and "how the output was checked" in ai,
                 "log covers direction, checking (replay inspection, mutation testing, simulator audit) and rejected suggestions")

    def score_poorly(self) -> None:
        s = "What will score poorly (must be absent)"
        comps = [d for d in self.decisions if d.get("composition")]
        grounded = all((d["composition"].get("by") == "rules") or d["composition"].get("candidates_offered", 0) > 0 for d in comps)
        self.add(s, "A single prompt that picks titles for every viewer", grounded,
                 f"{len(comps)} pages composed per viewer from that viewer's retrieved candidates; grounding checked")
        new_goals = [d for d in self.decisions if d.get("goal") and not d["goal"].get("kept_from_previous_decision")
                     and d["trigger"]["event"] != "control:title_withdrawn"]
        unsupported = [d for d in new_goals if not any(e.startswith("goal_rule:") for e in d["evidence"])
                       or not any(g["goal_id"] == d["goal"]["id"] and g["eligible"] for g in d["goal_evaluations"])]
        self.add(s, "Goals or eligibility asserted without evidence", not unsupported,
                 f"{len(new_goals)} committed goals; {len(unsupported)} without a passing eligibility evaluation and a cited rule")
        failed_safety = [c for c in self.out if c.section.startswith("9 Verification") and c.status == "FAIL"]
        self.add(s, "Messages in the wrong dialect, at night or without consent", not failed_safety,
                 "see the verification and safety checks above")
        self.add(s, "Uplift reported from served vs unserved viewers", "ITT" in self.impact.get("headline", {}).get("primary_metric", ""),
                 "headline is intent-to-treat on randomized arms; served-vs-unserved appears only as a warning")
        self.add(s, "Ignoring late or duplicate events", any(c.name.startswith("Triggers are idempotent") and c.status == "PASS" for c in self.out)
                 and any(c.name.startswith("Late (buffered)") and c.status == "PASS" for c in self.out),
                 "see the real-time checks above")
        corr = next((c for c in self.summary.get("controls", []) if c["type"] == "goal_rule_corrected"), None)
        self.add(s, "Reprocessing every viewer after one rule change",
                 corr is not None and corr["affected"].get("other_viewers_reprocessed") == 0,
                 f"G03 correction updated open plans {corr['affected'].get('open_plans_updated') if corr else '?'}; "
                 f"other viewers reprocessed: {corr['affected'].get('other_viewers_reprocessed') if corr else '?'}")
        ran = any(c.name == "Test suite passes" and c.status == "PASS" for c in self.out)
        self.add(s, "Submitting AI-generated code that was not run and tested",
                 ran if any(c.name == "Test suite passes" for c in self.out) else bool(self.decisions),
                 "test suite executed by this check" if ran else "sample_run was produced by the code; run with --run-tests to execute the suite")


def run_checks(sub: Path, pack: Path | None, run_tests: bool = False) -> list[Check]:
    c = Checker(sub, pack)
    c.submission()
    c.goal_quality()
    c.evidence_state()
    c.realtime()
    c.safety()
    c.measurement()
    c.engineering(run_tests)
    c.ai_tools()
    c.score_poorly()
    return c.out


def write_report(checks: list[Check], out: Path) -> dict[str, int]:
    counts = Counter(c.status for c in checks)
    icon = {"PASS": "✅ PASS", "FAIL": "❌ FAIL", "SKIP": "⏭ SKIP", "INFO": "ℹ INFO"}
    lines = ["# Evaluation check", "",
             "Generated by `python -m oua check` from the files in this repository and the sample run; nothing is typed by hand.", "",
             f"**{counts.get('PASS', 0)} passed, {counts.get('FAIL', 0)} failed, {counts.get('SKIP', 0)} skipped, "
             f"{counts.get('INFO', 0)} informational.**", ""]
    by_section: dict[str, list[Check]] = defaultdict(list)
    for c in checks:
        by_section[c.section].append(c)
    for section, items in by_section.items():
        lines += [f"## {section}", "", "| Check | Result | Evidence |", "|---|---|---|"]
        for c in items:
            lines.append(f"| {c.name} | {icon[c.status]} | {c.evidence.replace('|', '/')} |")
        lines.append("")
    out.write_text("\n".join(lines), encoding="utf-8")
    return dict(counts)
