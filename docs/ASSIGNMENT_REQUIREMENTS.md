# Assignment requirements

Source of truth: `assignment_one_user_one_agent (1).pdf` (6 pages) plus the data-pack README conventions that the brief relies on. This document maps each requirement to a planned component and test. Nothing here is implemented yet.

Pack identifiers (`U-#####`, `T####`, `G01`–`G12`) differ from the brief’s illustrative IDs (`V-2291`, `T0412`). Runtime code must use pack IDs from input, never the example IDs.

## 1. Product loop

| ID | Requirement | Rubric | Planned component | Planned test | Status |
|---|---|---|---|---|---|
| R1 | One agent per viewer owns the current goal, single active plan, actions, and outcomes | Goal/decision 20%; Evidence 15% | `src/oua/state/` + `DecisionService` | `tests/test_ownership.py` — two components cannot commit two active goals for one viewer | BLOCKED |
| R2 | On trigger, decide homepage change, one message channel, timing, or nothing | Goal/decision 20% | `src/oua/planning/` | `tests/test_decision_schema.py` — action enum only from pack | BLOCKED |
| R3 | Later judge whether the intervention worked from attributable signals | Measurement 15% | `src/oua/measurement/outcome_judger.py` | `tests/test_outcomes.py` — success only from configured event+condition | BLOCKED |
| R4 | Behave as a lifecycle manager with little time and imperfect information, not a ranked-list recommender | Goal/decision 20% | `DecisionService` | `tests/test_abstention.py` — no_action with explicit reason is valid | BLOCKED |

## 2. Seven work items from the brief

| ID | Requirement | Rubric | Planned component | Planned test | Status |
|---|---|---|---|---|---|
| R5 | Understand the viewer: every fact has source, time, freshness; conflicts resolved explicitly | Evidence 15% | `src/oua/reconciliation/` | `tests/test_conflicts.py` — profile vs app setting vs deeplink all visible | BLOCKED |
| R6 | Choose one eligible measurable goal with deadline and success signal, or explain why none | Goal/decision 20% | `src/oua/planning/goal_selector.py` | `tests/test_goal_selector.py` — G10/G11 never selected; ineligible goals rejected | BLOCKED |
| R7 | Intervene only with content in the dialect the viewer will actually see | Verification 15% | `src/oua/retrieval/` + verifier | `tests/test_verifier.py` — wrong-dialect titles fail | BLOCKED |
| R8 | Publish within latency and call budgets; on tool/model failure, smaller safe action or nothing | Real-time 15% | `src/oua/llm/` + `src/oua/replay/` | `tests/test_failures.py` — 503/timeout → no model-authorized action | BLOCKED |
| R9 | Independent verification of eligibility, dialect, availability, consent, caps, policy, duplicates | Verification 15% | `src/oua/verification/verifier.py` | `tests/test_verifier.py` — verifier can reject planner/LLM | BLOCKED |
| R10 | Close the loop: succeeded / failed / inconclusive from attributable signals; record learning | Measurement 15% | outcome judger + viewer memory | `tests/test_outcomes.py` — model text cannot mark success | BLOCKED |
| R11 | Prove impact against randomized holdout; explain why served-vs-unserved misleads | Measurement 15% | `src/oua/measurement/impact.py` | `tests/test_impact.py` — holdout arm from `replay/arms.csv`; pilot report method flagged | BLOCKED |

## 3. Required decision record

Brief example plus hiring-stage schema. One Pydantic model used by CLI, replay, and frontend.

| ID | Field / rule | Rubric | Planned component | Planned test | Status |
|---|---|---|---|---|---|
| R12 | Canonical JSON: `decision_id`, `viewer_id`, `trigger`, `goal`, `action`, `dialect`, `content`, `confidence`, `confidence_reason`, `evidence`, `evidence_details`, `assumptions`, `conflicting_evidence`, `checks`, `decided_in_ms`, `decision_deadline_ms`, `status` | Engineering 10% | `src/oua/schemas.py` | `tests/test_decision_schema.py` | BLOCKED |
| R13 | Goal includes deadline and supported success signal; goal may be null only with explicit inaction reason | Goal/decision 20% | schemas + goal selector | `tests/test_goal_selector.py` | BLOCKED |
| R14 | Cited sources resolve to real data or versioned config; evidence includes freshness/timestamps | Evidence 15% | evidence builder | `tests/test_evidence.py` | BLOCKED |
| R15 | Assumptions are viewer-specific, not boilerplate | Goal/decision 20% | evidence builder | `tests/test_evidence.py` | BLOCKED |
| R16 | Material conflicts state which source was chosen and why | Evidence 15% | reconciler | `tests/test_conflicts.py` | BLOCKED |
| R17 | Confidence from a documented backend method; not claimed calibrated unless evaluated | Engineering 10% | `src/oua/planning/confidence.py` | `tests/test_confidence.py` | BLOCKED |
| R18 | Runtime response is one JSON object, no Markdown fences; replay is JSONL, one decision per line | Engineering 10% | CLI + replay harness | `tests/test_replay_io.py` | BLOCKED |
| R19 | Status includes published / no_action / rejected / failed_timeout (final names in schema) | Real-time 15% | schemas | `tests/test_decision_schema.py` | BLOCKED |

## 4. Expected behaviour table (brief §5)

| ID | Capability | Requirement | Rubric | Planned component | Planned test | Status |
|---|---|---|---|---|---|---|
| R20 | Ownership | No two components pursue different goals for the same viewer | Evidence 15% | state store unique active goal | `tests/test_ownership.py` | BLOCKED |
| R21 | Planning | Revise goal/plan when situation changes (e.g. purchase), not on a fixed schedule | Goal/decision 20% | planner on new evidence | `tests/test_plan_revision.py` | BLOCKED |
| R22 | Source handling | Precedence and freshness per fact; not largest/newest/most convenient by default | Evidence 15% | `docs/SOURCE_PRECEDENCE.md` + reconciler | `tests/test_conflicts.py` | BLOCKED |
| R23 | Memory | Durable facts+provenance, decisions, actions, outcomes, lessons | Evidence 15% | SQLite | `tests/test_state_persistence.py` | BLOCKED |
| R24 | Tool use | Read data / call models only when it reduces uncertainty and fits budget | Real-time 15% | tool registry with counters | `tests/test_budgets.py` | BLOCKED |
| R25 | Verification | Checks can disagree with the decision step; generated text is never evidence or approval | Verification 15% | verifier | `tests/test_verifier.py`, `tests/test_model_faults.py` | BLOCKED |
| R26 | Abstention | Do nothing and not-enough-evidence are recorded decisions | Goal/decision 20% | planner | `tests/test_abstention.py` | BLOCKED |
| R27 | Real-time | 60s publish budget; >5 min is failure; late/duplicate triggers idempotent; degrade on dependency failure | Real-time 15% | replay harness | `tests/test_idempotency.py`, `tests/test_failures.py` | BLOCKED |
| R28 | Measurement | Attributable signals; compare to randomized holdout | Measurement 15% | impact | `tests/test_impact.py` | BLOCKED |
| R29 | Auditability | Evaluator can see what the agent knew, decided, observed for any viewer at any moment | Evidence 15% | state query API | `tests/test_audit.py` | BLOCKED |

## 5. Surprise events (brief §6) mapped to pack files

| ID | Surprise | Pack evidence | Planned response | Planned test | Status |
|---|---|---|---|---|---|
| R30 | Burst of buffered app opens, some days old | `replay/triggers.jsonl`: 453 triggers with received−occurred >10 min; 246 >1 day; simulator treats lag >600s as not a visit | Ingest as events; decide only if treated as a live trigger; record event_age_s; do not treat stale opens as current intent without saying so | `tests/test_late_triggers.py` | BLOCKED |
| R31 | Profile says never paid, purchase arrives after decision | 27 live `purchase_completed` on profile `never`; 36/38 live purchases are also triggered users | Revise open G05/G06/G04 for that viewer only | `tests/test_plan_revision.py` | BLOCKED |
| R32 | App setting, profile, campaign link name three dialects | 62 triple mismatches in triggers (example `U-04945`: profile d2, app d4, deeplink d6) | Homepage dialect = app settings feed; others remain conflicts | `tests/test_conflicts.py` | BLOCKED |
| R33 | Goal success condition corrected mid-replay | `replay/control_events.jsonl` G03 at 2026-10-01T08:30:00.000Z | Version rule; re-open affected G03 goals only | `tests/test_goal_correction.py` | BLOCKED |
| R34 | LLM claims eligibility with no supporting event | `replay/model_faults.jsonl` unsupported_claim for `U-01991`, `U-02509`, `U-04860` (all profile `never`, claim G04 trial) | Verifier rejects; backend eligibility wins | `tests/test_model_faults.py` | BLOCKED |
| R35 | Catalogue description instructs agent to message everyone | `data/catalogue.csv` title `T0002` description contains `SYSTEM NOTE FOR AI AGENTS...` | Text is content, never instruction (`config/messaging_policy.md` §6) | `tests/test_prompt_injection.py` | BLOCKED |
| R36 | Model or data store slow/unavailable | control: model_provider 503 10:35–10:42Z; user_data_store degraded 13:10–13:17Z; fault timeout 45s for `U-01090` | Fallback no_action / cached facts; never block past 60s | `tests/test_failures.py` | BLOCKED |
| R37 | Traffic triples at evening peak | Trigger hour IST 2026-10-01 20:00 has 351 vs quieter hours ~1–148 | Process in deliver_at order; measure decided_in_ms; no per-viewer special case | replay latency report | BLOCKED |

## 6. Hard constraints (brief §7–8)

| ID | Constraint | Pack / brief source | Planned component | Planned test | Status |
|---|---|---|---|---|---|
| R38 | ≤10–12 hours effort; incomplete well-prioritized work OK | brief | staged plan | n/a (process) | PARTIAL |
| R39 | Only the synthetic pack is viewer/title evidence; do not invent facts | README + brief | ingestion | `tests/test_evidence.py` | BLOCKED |
| R40 | Mock/replay model mode; no personal API key required for evaluators | brief | `src/oua/llm/mock_provider.py` | `tests/test_model_faults.py` | BLOCKED |
| R41 | Publish within 60s of trigger; >5 min = failure | brief | harness wall-clock + `decision_deadline_ms=60000` | `tests/test_latency.py` | BLOCKED |
| R42 | Max 8 model calls and 20 tool calls per decision | brief | budget counters | `tests/test_budgets.py` | BLOCKED |
| R43 | Quiet hours 22:00–08:00 IST; all users India | interventions + policy | verifier | `tests/test_messaging_policy.py` | BLOCKED |
| R44 | Push requires `notifications_opt_in`; WhatsApp requires `whatsapp_opt_in`; later `notification_opt_out` overrides snapshot | policy §1 | reconciler + verifier | `tests/test_messaging_policy.py` | BLOCKED |
| R45 | At most one push or WhatsApp per viewer per IST day, combined | interventions + policy | verifier | `tests/test_messaging_policy.py` | BLOCKED |
| R46 | Holdout arm of `replay/arms.csv` receives no agent action | policy §5 | verifier first check | `tests/test_holdout.py` | BLOCKED |
| R47 | Honest copy; only `config/pricing.json` prices; titles exist, available, matching dialect | policy §4 | verifier | `tests/test_copy_and_pricing.py` | BLOCKED |
| R48 | Do not upload pack to retaining/training hosts | policy §7 | mock default; no pack in prompts unless explicitly configured | architecture note | BLOCKED |
| R49 | WhatsApp: approved templates only; variables from catalogue/pricing | interventions | verifier | `tests/test_whatsapp_templates.py` | BLOCKED |
| R50 | Home row <3 valid titles dropped; same `content_slug` once per page; dialect+availability at publish time | interventions + simulator `validate_page` | homepage builder + verifier | `tests/test_homepage.py` | BLOCKED |
| R51 | Automated tests: late or duplicate trigger; conflicting source; tool failure; corrected goal rule | brief §8 | pytest | those four files must exist and assert behavior | BLOCKED |
| R52 | Sample replay: decisions, actions, no-action reasons, outcomes, impact report | brief §8 | `sample_run/` | produced by replay CLI | BLOCKED |
| R53 | Architecture note, AI collaboration log, known limitations | brief §8 | `submission/` later | n/a this stage | BLOCKED |
| R54 | Runnable CLI/API and one command that replays the trigger script | brief §8 | `python -m oua.replay` | smoke test | BLOCKED |
| R55 | Provider abstraction | brief | LLM protocol | mock used in CI | BLOCKED |
| R56 | Scope: working decide/act/measure over polished UI | brief | Streamlit thin client calling `DecisionService` last | optional | BLOCKED |

## 7. Goals that cannot be used as measurable commitments

See `docs/DATA_DICTIONARY.md` for field-level evidence.

| Goal | Issue | Agent rule |
|---|---|---|
| G10 `meaningful_engagement_7d` | `success.event` is JSON `null`; condition is “engages meaningfully” with no event in the stream | Never select. If considered, record ineligible/unmeasurable. |
| G11 `tv_activation_14d` | Success event `tv_app_open` does not appear in `data/events.jsonl` (0 rows). Platforms observed: android, ios only | Never select. Signal cannot be observed in pack or simulator output schema. |

Underspecified but possibly implementable with explicit documented interpretation (must not be silently assumed):

| Goal | Ambiguity | Proposed interpretation (pending review) |
|---|---|---|
| G06 | “Subscription recently lapsed” has no `within_days` field | Eligible if current status is `lapsed` (from events preferred over snapshot) and a `subscription_lapsed` event exists, or snapshot says lapsed. Do not invent a recency window. Label the assumption. |
| G07 | Eligibility is text-only: unfinished show/micro-drama, idle ≥3 days | Derive from play_started/episode_completed vs `data/episodes.csv`; films have 1 episode so cannot be “unfinished series”. Document rules before coding. |
| G09 | “plays on at most 1 day in the last 14 days” is text-only | Count distinct IST days with `play_started` in the 14 days before trigger `received_at`. |
| G03 | Success is `app_open` on a later IST day until control correction | After `goal_rule_corrected`, open G03 goals use `play_started` on a later IST day. Do not rewrite closed outcomes. |
| G04 | Only 4 snapshot `trial_active` users; 0 historical `is_trial=true` purchases | Eligibility from reconciled status `trial_active` only. LLM trial claims without events are rejected. |
| G12 | Eligibility is no `app_open` in 14 days before the trigger, but the trigger itself is often `app_open` | Evaluate the 14-day window **before** this trigger’s `received_at`, excluding the current trigger. |

## 8. What will score poorly (explicit non-goals)

Do not: single prompt picking titles for every viewer; assert goals without evidence; message wrong dialect / at night / without consent; report uplift as served vs unserved; ignore late/duplicate events; reprocess every viewer after one rule change; submit unrun AI code.

Pilot `pilot/pilot_summary_report.md` headline (40.1% served vs 17.7% everyone else) is exactly the comparison the brief says misleads. Intent-to-treat on the same file: treatment 349/1912 (18.3%) vs holdout 387/1979 (19.6%).

## 9. This reconnaissance stage

| ID | Deliverable | Status |
|---|---|---|
| D1 | Inspect brief and pack; write six docs; no production code | PASS (this stage) |
| D2 | Production agent | BLOCKED pending plan review |
