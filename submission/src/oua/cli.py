"""Command line: replay, simulate, judge, impact, audit, or everything at once with `run-all`."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

from oua.settings import SUBMISSION_ROOT, Settings


def _settings(a) -> Settings:
    return Settings.load(pack_path=a.pack, db_path=a.db, workers=getattr(a, "workers", None),
                         llm_provider=getattr(a, "llm", None), llm_model=getattr(a, "llm_model", None))


def _open(s: Settings, fresh: bool):
    from oua.pack import Pack, load_pack_into_store
    from oua.store import Store

    if fresh:
        for suffix in ("", "-wal", "-shm"):
            p = Path(str(s.db_path) + suffix)
            if p.exists():
                p.unlink()
    pack = Pack.open(s.pack_path)
    store = Store(s.db_path)
    stats = None
    if fresh:
        stats = load_pack_into_store(pack, store)
    else:
        from collections import Counter
        plays: Counter = Counter()
        for row in store.conn.execute("SELECT properties FROM events WHERE event = 'play_started' AND source = 'history'"):
            t = pack.catalogue.get(json.loads(row["properties"]).get("title_id"))
            if t:
                plays[t.content_slug] += 1
        pack.catalogue.plays_by_slug = plays
    return pack, store, stats


def cmd_replay(a) -> dict:
    from oua.agent import AgentService
    from oua.harness import run_replay

    s = _settings(a)
    t0 = time.perf_counter()
    pack, store, stats = _open(s, fresh=True)
    print(f"loaded pack in {time.perf_counter() - t0:.1f}s: {stats}", file=sys.stderr)
    service = AgentService(s, store, pack)
    summary = run_replay(service, pack, Path(a.out), s.workers, log=lambda m: print(m, file=sys.stderr))
    print(f"replay done in {time.perf_counter() - t0:.1f}s: {summary['decisions']} decisions, "
          f"{summary['actions']} actions, {summary['redeliveries']} redeliveries", file=sys.stderr)
    store.close()
    return summary


def cmd_simulate(a) -> None:
    s = _settings(a)
    sim = s.pack_path / "simulator" / "simulate_outcomes.py"
    out = Path(a.out)
    cmd = [sys.executable, "-I", str(sim), "--pack", str(s.pack_path), "--actions", str(out / "actions.jsonl"),
           "--out", str(out / "sim"), "--seed", a.seed]
    print("running simulator:", " ".join(cmd), file=sys.stderr)
    subprocess.run(cmd, check=True)


def cmd_judge(a) -> None:
    from oua.measure import judge_all

    s = _settings(a)
    pack, store, _ = _open(s, fresh=False)
    res = judge_all(store, pack, Path(a.out))
    print(f"judged {res['plans']} plans: {res['status']}", file=sys.stderr)
    store.close()


def cmd_impact(a) -> None:
    from oua.measure import impact_report, write_final_report, write_viewer_state

    s = _settings(a)
    pack, store, _ = _open(s, fresh=False)
    out = Path(a.out)
    impact = impact_report(store, pack, out)
    write_viewer_state(store, out / "viewer_state")
    write_final_report(store, pack, out, impact, s)
    print(f"impact: {json.dumps(impact['headline'])}", file=sys.stderr)
    store.close()


def cmd_audit(a) -> None:
    from oua.measure import audit

    s = _settings(a)
    pack, store, _ = _open(s, fresh=False)
    print(json.dumps(audit(store, a.viewer, a.at), indent=2, ensure_ascii=False, default=str))
    store.close()


def cmd_check(a) -> None:
    from oua.check import run_checks, write_report

    s = _settings(a)
    out = Path(a.out) / "evaluation_check.md"
    checks = run_checks(SUBMISSION_ROOT, s.pack_path, run_tests=a.run_tests)
    counts = write_report(checks, out)
    width = max(len(c.name) for c in checks)
    for c in checks:
        print(f"{c.status:5}  {c.name:<{width}}  {c.evidence[:110]}")
    print()
    print(f"{counts.get('PASS', 0)} passed, {counts.get('FAIL', 0)} failed, {counts.get('SKIP', 0)} skipped -> {out}")
    if counts.get("FAIL"):
        sys.exit(1)


def cmd_run_all(a) -> None:
    cmd_replay(a)
    cmd_simulate(a)
    cmd_judge(a)
    cmd_impact(a)


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="oua", description=__doc__)
    p.add_argument("--pack", type=Path, default=None, help="data pack root (default: parent of submission/)")
    p.add_argument("--db", type=Path, default=None, help="SQLite state file (default: viewer_state/agent.sqlite)")
    sub = p.add_subparsers(dest="cmd", required=True)

    def common(sp, out=True):
        if out:
            sp.add_argument("--out", default=str(SUBMISSION_ROOT / "sample_run"))
        sp.add_argument("--seed", default="oua-practice")
        sp.add_argument("--workers", type=int, default=None)
        sp.add_argument("--llm", choices=["mock", "anthropic"], default=None,
                        help="model provider (default from config/agent.json: mock; anthropic needs ANTHROPIC_API_KEY)")
        sp.add_argument("--llm-model", default=None, help="Claude model id when --llm anthropic (default claude-opus-5-5)")

    for name, fn, helptext in (
        ("replay", cmd_replay, "replay the trigger script through the agent (fresh state)"),
        ("simulate", cmd_simulate, "run the pack's outcome simulator on the published actions"),
        ("judge", cmd_judge, "judge every plan from attributable signals; write lessons"),
        ("impact", cmd_impact, "holdout impact report, viewer state export, final report"),
        ("run-all", cmd_run_all, "replay + simulate + judge + impact"),
    ):
        sp = sub.add_parser(name, help=helptext)
        common(sp)
        sp.set_defaults(fn=fn)
    sp = sub.add_parser("check", help="evaluation check: verify the submission against the brief (sections 8 and 9)")
    sp.add_argument("--out", default=str(SUBMISSION_ROOT / "sample_run"))
    sp.add_argument("--run-tests", action="store_true", help="also run the test suite")
    sp.set_defaults(fn=cmd_check)
    sp = sub.add_parser("audit", help="what the agent knew, decided and observed for a viewer at a moment")
    sp.add_argument("--viewer", required=True)
    sp.add_argument("--at", default=None, help="ISO time (default: everything)")
    sp.set_defaults(fn=cmd_audit)
    a = p.parse_args(argv)
    a.fn(a)


if __name__ == "__main__":
    main()
