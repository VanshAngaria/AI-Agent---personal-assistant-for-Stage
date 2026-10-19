# Final report: sample replay

Pack: `one_user_one_agent_pack`; replay day 2026-10-01 IST; simulator seed in `sim/simulator_report.json` (`oua-practice`).

## What happened

* Triggers delivered: 2769 (90 were duplicate deliveries answered with the stored decision).
* Decisions: 2687 (including 8 page repairs triggered by a title withdrawal).
* Status by arm: agent/FAILED_SAFE: 2, agent/NO_ACTION: 440, agent/PUBLISHED: 920, holdout/FAILED_SAFE: 5, holdout/NO_ACTION: 1320.
* Reasons for no action: holdout: 1061, stale_trigger: 424, page_already_live: 144, no_dialect: 91, no_goal: 40, data_store: 7.
* Goals committed: G07: 632, G05: 309, G08: 171, G01: 109, G12: 80, G02: 68, G09: 59, G06: 44, G03: 3, G04: 2 (G10/G11 never: unmeasurable).
* Actions published: home_page: 920, push: 223, whatsapp: 11.
* Simulator policy problems reported for our actions: **0**; holdout actions ignored: 0; pages accepted 920, rows dropped 0.

## Latency

* p50 0.441s, p95 1.246s, max 6.947s (budget 60s; failure after 300s). Over budget: 0; over 5 min: 0.
* Pages published within the 3.0s page target: 910/912.
* Peak hour 2026-10-01 10:00 IST: 336 triggers, p95 0.848s.
* Max calls in one decision: tool 9/20, model 3/8.

## Surprise events and how the agent responded

| Surprise | What the agent did | Where to look |
|---|---|---|
| Buffered offline opens | 424 triggers had device buffer time > 600s; recorded as history, no page, no plan change | D-000027 (U-03644), D-000028 (U-03547), D-000051 (U-03214) |
| Duplicate deliveries | 90 redeliveries returned the stored decision; no second decision or action | `processed_triggers` table |
| Three-way dialect conflict | app setting used; profile and campaign-link dialects kept as conflicts | D-000004 (U-02905), D-000016 (U-04945), D-000030 (U-01822) |
| T0001 withdrawn at 2026-10-01T07:00:00.000Z | excluded from retrieval and verification from that moment; 8 live page(s) repaired by republishing without it | D-000835, D-000836, D-000837, D-000838, D-000839, D-000840, D-000841, D-000842 |
| G03 success rule corrected at 2026-10-01T08:30:00.000Z | new version G03@2026-09-30.r2; 0 open plan(s) updated, 0 other viewers reprocessed; closed outcomes untouched | `control_log`, `plan_history` |
| model_provider unavailable 2026-10-01T10:35:00.000Z to 2026-10-01T10:42:00.000Z | 21 decisions in the window: {'PUBLISHED': 10, 'NO_ACTION': 11}; model outcomes {'skipped': 10, 'unavailable': 11}; max latency 0.099s | `latency_report.json` |
| user_data_store degraded 2026-10-01T13:10:00.000Z to 2026-10-01T13:17:00.000Z | 16 decisions in the window: {'FAILED_SAFE': 7, 'PUBLISHED': 3, 'NO_ACTION': 6}; model outcomes {'skipped': 12, 'skipped_for_latency': 3, 'agreed': 1}; max latency 6.947s | `latency_report.json` |
| Purchase arrives right after a decision | live events close the open plan at once (succeeded if attributable) or flag it for re-planning; 13 plans closed by live events | `plan_history` |
| Catalogue description with instructions | the title is quarantined from promotion; its text is never passed to the planner or model as an instruction | `catalogue.py`, `tests/test_safety.py` |

Model faults from `replay/model_faults.jsonl`:

| Decision | Viewer | Fault | Status | Action | Model |
|---|---|---|---|---|---|
| D-000426 | U-04860 | unsupported_claim | NO_ACTION | stale_trigger | model: skipped |
| D-000563 | U-02509 | unsupported_claim | NO_ACTION | stale_trigger | model: skipped |
| D-000886 | U-01991 | unsupported_claim | PUBLISHED | HOME_PAGE | model: rejected_unsupported |
| D-001356 | U-01090 | timeout | NO_ACTION | holdout | model: skipped |
| D-001375 | U-02563 | malformed_output | PUBLISHED | HOME_PAGE | model: malformed |

Faults on buffered triggers never reach the model: the agent abstains before any model call.

## Outcomes

* agent/failed: 651, agent/succeeded: 99, holdout/failed: 638, holdout/succeeded: 89

## Impact

* Primary (ITT, first goal): agent 13.3% vs holdout 12.3%: +1.0 pts (95% CI -2.4 to +4.4; p=0.574).
* Full analysis, including why served-vs-unserved misleads and the recomputed pilot: `impact_report.md`.

## Files

`decisions.jsonl` (every decision), `actions.jsonl` (simulator input), `outcomes.jsonl` (every plan judged), `viewer_state/` (per-viewer durable state export), `latency_report.json`, `impact_report.md`, `replay_summary.json`, `sim/simulator_report.json`.
