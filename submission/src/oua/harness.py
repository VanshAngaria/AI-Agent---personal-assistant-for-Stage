"""Replay harness: feeds triggers, live events and control events to the agent in delivery order.

Virtual time: each decision starts when a worker is free (pool of `settings.workers`) and the viewer's previous
decision has finished (one agent per viewer => a viewer's triggers never run concurrently).
published_at = deliver_at + queue wait + simulated dependency time + measured CPU time.
"""

from __future__ import annotations

import heapq
import json
import statistics
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

from oua.agent import AgentService
from oua.pack import Pack
from oua.timeutil import IST, iso, parse_ts

PRIORITY = {"control": 0, "live": 1, "trigger": 2}


def timeline(pack: Pack) -> list[tuple[datetime, int, int, str, dict[str, Any]]]:
    items = []
    seq = 0
    for c in pack.control_events():
        items.append((parse_ts(c["at"]), PRIORITY["control"], seq, "control", c))
        seq += 1
    for e in pack.live_events():
        items.append((parse_ts(e.get("deliver_at") or e["received_at"]), PRIORITY["live"], seq, "live", e))
        seq += 1
    for t in pack.triggers():
        items.append((parse_ts(t.get("deliver_at") or t["received_at"]), PRIORITY["trigger"], seq, "trigger", t))
        seq += 1
    items.sort(key=lambda x: (x[0], x[1], x[2]))
    return items


def run_replay(service: AgentService, pack: Pack, out_dir: Path, workers: int, log=print) -> dict[str, Any]:
    out_dir.mkdir(parents=True, exist_ok=True)
    items = timeline(pack)
    pool: list[datetime] = []
    viewer_free: dict[str, datetime] = {}
    redeliveries: list[dict[str, Any]] = []
    live_results: Counter = Counter()
    controls: list[dict[str, Any]] = []
    n = 0
    for at, _, _, kind, payload in items:
        if kind == "control":
            controls.append({"at": payload["at"], "type": payload["type"], "affected": service.on_control(payload)})
            continue
        if kind == "live":
            r = service.on_live_event(payload)
            live_results["duplicate" if r["duplicate"] else "ingested"] += 1
            if r.get("plan_closed"):
                live_results[f"plan_closed_{r['plan_closed']}"] += 1
            continue
        if len(pool) < workers:
            free = at
        else:
            free = heapq.heappop(pool)
        start = max(at, free, viewer_free.get(payload["user_id"], at))
        res = service.handle_trigger(payload, start_at=start)
        if res["redelivery"]:
            redeliveries.append(res)
            heapq.heappush(pool, free)
            continue
        done = parse_ts(res["decision"]["trigger"]["processing_started_at"])
        finished = done + _ms(res["decision"]["decided_in_ms"])
        heapq.heappush(pool, finished)
        viewer_free[payload["user_id"]] = finished
        n += 1
        if n % 500 == 0:
            log(f"  {n} decisions...")

    decisions = service.store.all_decisions()
    actions = service.store.all_actions()
    _write_jsonl(out_dir / "decisions.jsonl", decisions)
    _write_jsonl(out_dir / "actions.jsonl", [_sim_action(a) for a in actions])
    report = latency_report(decisions, redeliveries, service)
    (out_dir / "latency_report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    summary = {"decisions": len(decisions), "actions": len(actions), "redeliveries": len(redeliveries),
               "live_events": dict(live_results), "controls": controls}
    (out_dir / "replay_summary.json").write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")
    return summary


def _ms(ms: int):
    from datetime import timedelta
    return timedelta(milliseconds=ms)


def _sim_action(a: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in a.items()}


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    with open(path, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def _pct(values: list[float], q: float) -> float | None:
    if not values:
        return None
    values = sorted(values)
    k = min(len(values) - 1, max(0, int(round(q * (len(values) - 1)))))
    return values[k]


def latency_report(decisions: list[dict[str, Any]], redeliveries: list[dict[str, Any]], service: AgentService) -> dict[str, Any]:
    real = [d for d in decisions if not d["trigger"]["event"].startswith("control:")]
    lat = [d["latency_ms"] / 1000 for d in real]
    pub = [d for d in real if d["status"] == "PUBLISHED"]
    pages = [d for d in pub if d["action"] == "HOME_PAGE"]
    by_hour: dict[str, list[float]] = defaultdict(list)
    for d in real:
        h = parse_ts(d["trigger"]["deliver_at"]).astimezone(IST).strftime("%Y-%m-%d %H:00 IST")
        by_hour[h].append(d["latency_ms"] / 1000)
    windows = []
    for w in service.faults.windows:
        inside = [d for d in real if w.start <= parse_ts(d["trigger"]["processing_started_at"]) < w.end]
        windows.append({
            "dependency": w.dependency, "mode": w.kind, "from": iso(w.start), "until": iso(w.end),
            "decisions": len(inside),
            "status": dict(Counter(d["status"] for d in inside)),
            "model_outcomes": dict(Counter(d["model"]["outcome"] for d in inside)),
            "p95_latency_s": _pct([d["latency_ms"] / 1000 for d in inside], 0.95),
            "max_latency_s": max([d["latency_ms"] / 1000 for d in inside], default=None),
        })
    peak = max(by_hour.items(), key=lambda kv: len(kv[1])) if by_hour else (None, [])
    return {
        "budget_s": service.s.decision_deadline_s,
        "failure_after_s": service.s.failure_after_s,
        "page_target_s": service.s.page_target_s,
        "workers": service.s.workers,
        "decisions": len(real),
        "redeliveries_answered_idempotently": len(redeliveries),
        "status_counts": dict(Counter(d["status"] for d in real)),
        "latency_s": {"p50": _pct(lat, 0.5), "p95": _pct(lat, 0.95), "p99": _pct(lat, 0.99), "max": max(lat, default=None),
                      "mean": round(statistics.mean(lat), 3) if lat else None},
        "queue_wait_s": {"p95": _pct([d["queue_wait_ms"] / 1000 for d in real], 0.95),
                         "max": max([d["queue_wait_ms"] / 1000 for d in real], default=None)},
        "cpu_ms": {"p50": _pct([d["cpu_ms"] for d in real], 0.5), "p95": _pct([d["cpu_ms"] for d in real], 0.95)},
        "over_budget_60s": sum(1 for x in lat if x > service.s.decision_deadline_s),
        "over_failure_300s": sum(1 for x in lat if x > service.s.failure_after_s),
        "pages_published": len(pages),
        "pages_within_page_target": sum(1 for d in pages if d["latency_ms"] / 1000 <= service.s.page_target_s),
        "tool_calls": {"max": max((d["budget"]["tool_calls"] for d in real), default=0),
                       "limit": service.s.max_tool_calls},
        "model_calls": {"max": max((d["budget"]["model_calls"] for d in real), default=0),
                        "total": sum(d["budget"]["model_calls"] for d in real), "limit": service.s.max_model_calls},
        "peak_hour": {"hour": peak[0], "triggers": len(peak[1]), "p95_latency_s": _pct(peak[1], 0.95)},
        "by_hour": {h: {"triggers": len(v), "p95_latency_s": _pct(v, 0.95)} for h, v in sorted(by_hour.items())},
        "dependency_windows": windows,
    }
