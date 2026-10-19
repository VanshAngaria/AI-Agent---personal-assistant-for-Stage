# Evaluation matrix

Rubric weights from the brief (must be used as build priority):

| Area | Weight |
|---|---|
| Goal and decision quality | 20% |
| Evidence and state | 15% |
| Real-time reliability | 15% |
| Verification and safety | 15% |
| Measurement and learning | 15% |
| Engineering quality | 10% |
| Use of AI tools | 10% |

Status values: `PASS` | `PARTIAL` | `FAIL` | `BLOCKED`.

Status is earned only by running behavior, except reconnaissance documentation rows.

Machine-readable rows (pipe table). A generator must count these rows rather than hardcoding totals.

```matrix
id|requirement|rubric|component|test|status|evidence
R1|One agent owns goal/plan/history|Evidence and state|state + DecisionService|tests/test_ownership.py|BLOCKED|not built
R2|Decide homepage, one message, timing, or nothing|Goal and decision quality|planning|tests/test_decision_schema.py|BLOCKED|not built
R3|Judge outcomes from attributable signals|Measurement and learning|measurement/outcome_judger.py|tests/test_outcomes.py|BLOCKED|not built
R4|Lifecycle manager not ranked-list recommender|Goal and decision quality|DecisionService|tests/test_abstention.py|BLOCKED|not built
R5|Facts with source, time, freshness; explicit conflicts|Evidence and state|reconciliation|tests/test_conflicts.py|BLOCKED|not built
R6|One eligible measurable goal or explicit none|Goal and decision quality|planning/goal_selector.py|tests/test_goal_selector.py|BLOCKED|not built
R7|Content in dialect viewer will see|Verification and safety|retrieval + verifier|tests/test_verifier.py|BLOCKED|not built
R8|Latency/call budgets; fail small or nothing|Real-time reliability|llm + replay|tests/test_failures.py|BLOCKED|not built
R9|Independent verification gate|Verification and safety|verification/verifier.py|tests/test_verifier.py|BLOCKED|not built
R10|Close loop succeeded/failed/inconclusive + learning|Measurement and learning|outcome judger + memory|tests/test_outcomes.py|BLOCKED|not built
R11|Impact vs randomized holdout; reject served-vs-unserved|Measurement and learning|measurement/impact.py|tests/test_impact.py|BLOCKED|pilot CSV ITT computed in recon only
R12|Canonical decision schema (stage fields)|Engineering quality|schemas.py|tests/test_decision_schema.py|BLOCKED|not built
R13|Goal deadline + supported success signal|Goal and decision quality|schemas + goal_selector|tests/test_goal_selector.py|BLOCKED|not built
R14|Evidence resolves to real data; freshness|Evidence and state|evidence builder|tests/test_evidence.py|BLOCKED|not built
R15|Non-boilerplate assumptions|Goal and decision quality|evidence builder|tests/test_evidence.py|BLOCKED|not built
R16|Conflicts: chosen source and why|Evidence and state|reconciler|tests/test_conflicts.py|BLOCKED|not built
R17|Documented backend confidence; not fake calibration|Engineering quality|planning/confidence.py|tests/test_confidence.py|BLOCKED|not built
R18|Single JSON object / JSONL replay|Engineering quality|cli + harness|tests/test_replay_io.py|BLOCKED|not built
R19|Decision status enum|Real-time reliability|schemas.py|tests/test_decision_schema.py|BLOCKED|not built
R20|No dual active goals|Evidence and state|state unique plan|tests/test_ownership.py|BLOCKED|not built
R21|Revise plan on situation change|Goal and decision quality|planner|tests/test_plan_revision.py|BLOCKED|not built
R22|Per-fact precedence not newest-wins default|Evidence and state|reconciler|tests/test_conflicts.py|BLOCKED|not built
R23|Durable memory|Evidence and state|SQLite|tests/test_state_persistence.py|BLOCKED|not built
R24|Tools/models only if useful and in budget|Real-time reliability|tools/registry.py|tests/test_budgets.py|BLOCKED|not built
R25|Verifier can disagree; LLM text not evidence|Verification and safety|verifier|tests/test_model_faults.py|BLOCKED|not built
R26|Abstention recorded|Goal and decision quality|planner|tests/test_abstention.py|BLOCKED|not built
R27|60s publish; 5min failure; idempotent late/dup; degrade|Real-time reliability|harness|tests/test_idempotency.py|BLOCKED|not built
R28|Holdout measurement|Measurement and learning|impact.py|tests/test_impact.py|BLOCKED|not built
R29|Audit viewer at a moment|Evidence and state|state query|tests/test_audit.py|BLOCKED|not built
R30|Buffered/late app opens|Real-time reliability|ingestion + harness|tests/test_late_triggers.py|BLOCKED|pack: 453 triggers lag>10m
R31|Purchase after never-paid profile|Measurement and learning|plan revision|tests/test_plan_revision.py|BLOCKED|pack: 27 live purchases on profile never
R32|Three-dialect conflict|Evidence and state|reconciler|tests/test_conflicts.py|BLOCKED|pack: 62 triple mismatches
R33|Mid-replay G03 correction|Measurement and learning|rule_versions|tests/test_goal_correction.py|BLOCKED|control_events.jsonl
R34|Unsupported LLM eligibility|Verification and safety|mock + verifier|tests/test_model_faults.py|BLOCKED|model_faults unsupported_claim
R35|Catalogue prompt injection|Verification and safety|retrieval + verifier|tests/test_prompt_injection.py|BLOCKED|T0002 description
R36|Model/store outage|Real-time reliability|mock + harness|tests/test_failures.py|BLOCKED|control 503 and degraded store
R37|Peak traffic|Real-time reliability|harness latency_report|sample_run/latency_report.json|BLOCKED|IST hour 20:00 has 351 triggers
R38|Time-box 10-12h prioritized scope|Engineering quality|staged plan|docs/IMPLEMENTATION_SEQUENCE.md|PARTIAL|plan exists; build not started
R39|Pack is only viewer/title evidence|Evidence and state|ingestion|tests/test_evidence.py|BLOCKED|not built
R40|Mock/replay LLM; no personal API key|Engineering quality|llm/mock_provider.py|tests/test_model_faults.py|BLOCKED|not built
R41|Publish <=60s; >5min failure|Real-time reliability|harness|tests/test_latency.py|BLOCKED|not built
R42|<=8 model and <=20 tool calls|Real-time reliability|tools/registry.py|tests/test_budgets.py|BLOCKED|not built
R43|Quiet hours 22:00-08:00 IST|Verification and safety|verifier|tests/test_messaging_policy.py|BLOCKED|not built
R44|Consent + opt-out override|Verification and safety|reconciler + verifier|tests/test_messaging_policy.py|BLOCKED|not built
R45|One push or WhatsApp per IST day combined|Verification and safety|verifier|tests/test_messaging_policy.py|BLOCKED|not built
R46|replay/arms.csv holdout isolation|Verification and safety|verifier|tests/test_holdout.py|BLOCKED|not built
R47|Honest copy and pricing.json only|Verification and safety|verifier|tests/test_copy_and_pricing.py|BLOCKED|not built
R48|Do not upload pack to retaining hosts|Use of AI tools|mock default|docs/ARCHITECTURE_PLAN.md|PARTIAL|recon constraint recorded; runtime not built
R49|Approved WhatsApp templates only|Verification and safety|verifier|tests/test_whatsapp_templates.py|BLOCKED|not built
R50|Homepage row/slug/availability rules|Verification and safety|homepage + verifier|tests/test_homepage.py|BLOCKED|not built
R51|Required automated tests exist|Engineering quality|pytest|four required cases|BLOCKED|not built
R52|Sample replay artifacts|Engineering quality|replay CLI|sample_run/*|BLOCKED|not built
R53|Architecture, AI log, limitations notes|Use of AI tools|submission/|written after build|BLOCKED|not built
R54|Runnable replay command|Engineering quality|python -m oua.replay|smoke|BLOCKED|not built
R55|Provider abstraction|Engineering quality|llm protocol|mock in CI|BLOCKED|not built
R56|UI not a substitute for the loop|Engineering quality|Streamlit last|tests/test_single_viewer_service.py|BLOCKED|not built
R57|G10 never committed (null success event)|Goal and decision quality|goal_selector|tests/test_goal_selector.py|BLOCKED|goals.json success.event null
R58|G11 never committed (tv_app_open absent)|Goal and decision quality|goal_selector|tests/test_goal_selector.py|BLOCKED|0 tv_app_open events
R59|No viewer-specific hardcoded decisions|Engineering quality|all modules|grep/tests on fixtures|BLOCKED|not built
R60|Shared backend for CLI, replay, frontend|Engineering quality|service.py|import tests|BLOCKED|not built
D0|Recon docs from inspected files only|Engineering quality|docs/*|this matrix|PASS|docs written 2026-10-19; no production code
```

## Counts

The following counts are produced by parsing the `matrix` fence above (see command in this file). Do not edit the numbers by hand if the table changes; re-run the counter.

```
PASS: 1
PARTIAL: 2
FAIL: 0
BLOCKED: 58
total: 61
```

Counter (run from repo root):

```python
from pathlib import Path
from collections import Counter
text = Path("docs/EVALUATION_MATRIX.md").read_text(encoding="utf-8")
body = text.split("```matrix")[1].split("```")[0].strip().splitlines()[1:]
c = Counter(line.split("|")[5] for line in body if line.strip())
print(dict(c), "total", sum(c.values()))
```

## Priority order for the next coding stage

1. Goal/decision (20%): selector + abstention + no G10/G11.
2. Tie 15%: evidence/state, realtime idempotency, verifier, holdout measurement — stages 2–7 and 10.
3. Engineering (10%): one schema, one service, tests that fail on wrong behavior.
4. Use of AI tools (10%): mock, injection resistance, honest collaboration log later.

## Unmeasurable / ambiguous items (do not mark PASS later without a test)

| Item | Matrix impact |
|---|---|
| G10, G11 | PASS only when tests prove they are never selected and never judged succeeded |
| G06 recency, G07 series rules | PARTIAL until interpretation is coded and tested |
| In-app payload | PARTIAL if we abstain from in-app with documented reason |
| Live `app_open` not in triggers | PASS if tests show no extra decisions |
| Confidence calibration | Must remain unclaimed unless a calibration study exists |
