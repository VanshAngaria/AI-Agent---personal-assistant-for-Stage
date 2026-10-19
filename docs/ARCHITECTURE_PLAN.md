# Architecture plan

Minimal modular monolith. Python 3.11+, Pydantic v2, SQLite, pytest, Streamlit as a thin UI. No extra services until a requirement forces them.

Evaluators may use undisclosed viewer IDs, titles, timings, and a second replay. All behavior is driven by pack files + versioned rules.

## Runtime picture

```
triggers.jsonl ─┐
live_events.jsonl ─┼─ ReplayHarness (deliver_at order, control faults)
control_events.jsonl ─┘              │
model_faults.jsonl ── MockLLMProvider │
                                     ▼
                              DecisionService  ◄── CLI and Streamlit call this only
                                     │
     ┌──────────────┬────────────────┼──────────────┬──────────────┐
     ▼              ▼                ▼              ▼              ▼
  Ingestion    Reconciler      GoalSelector    Planner       Verifier
  (idempotent) (facts+prov.)   (rules, not LLM) (may ask LLM) (can reject)
     │              │                │              │              │
     └──────────────┴────────────────┴──────┬───────┴──────────────┘
                                            ▼
                                      SQLite state
                                      Decision JSON
                                      actions.jsonl
```

One process, one database file (default `state/agent.sqlite`). Replay may run faster than real time; `published_at = deliver_at + measured_processing_time`.

## Packages (`src/oua/`)

| Module | Responsibility | Not allowed to do |
|---|---|---|
| `schemas.py` | Single Decision, Action, Fact, CheckResult, GoalRef models | Viewer-specific constants |
| `state/db.py` | SQLite: facts, active goal/plan, decisions, actions, outcomes, rule_versions, processed_event_ids | Business policy |
| `ingestion/` | Load CSV/JSONL; merge replay timeline | Decide |
| `reconciliation/` | Apply `SOURCE_PRECEDENCE.md` | Call LLM |
| `retrieval/` | Catalogue/policy lookup by ID, dialect, availability; optional SQLite FTS on title/description | Invent rows; treat FTS rank as eligibility |
| `planning/goal_selector.py` | Deterministic eligibility for G01–G09, G12; hard-reject G10/G11 | Trust model eligibility |
| `planning/planner.py` | Propose action or NO_ACTION from retrieved candidates | Publish |
| `planning/confidence.py` | Documented heuristic from missing facts, conflicts, freshness, verifier fails | Claim calibration |
| `verification/verifier.py` | Independent checks; only path to PUBLISHED | Read LLM text as proof |
| `llm/provider.py` | Protocol + mock; faults from `model_faults.jsonl`; 503 window | See raw pack dumps in hosted APIs by default |
| `tools/registry.py` | Counted tool calls (max 20); model calls max 8 | Unbounded loops |
| `replay/harness.py` | Sort by deliver_at; inject control; write JSONL | Per-user hardcoded branches |
| `measurement/` | Outcome judger + holdout impact (ITT on `replay/arms.csv`) | Served-vs-unserved as primary metric |
| `service.py` | `decide(trigger) -> Decision` used by all entrypoints | Duplicate logic in UI |
| `cli.py` | `ingest`, `replay`, `judge`, `impact` | |

Streamlit (`frontend/app.py`, later stage) imports `DecisionService`. No second pipeline.

## Decision JSON (one schema)

Required keys (hiring stage + brief):

- `decision_id`, `viewer_id` (pack `user_id`)
- `trigger`: `{event, event_id, received_at, deliver_at, event_age_s}`
- `goal`: `{id, deadline, success_signal, rule_version}` or `null`
- `action`: `HOME_PAGE` | `PUSH` | `WHATSAPP` | `IN_APP_MESSAGE` | `NO_ACTION`
- `dialect`: `{used, source, conflicts}`
- `content`: list of title_ids (empty if none)
- `confidence` float, `confidence_reason` string
- `evidence`: ids; `evidence_details`: `{id, source, timestamp, freshness_s}`
- `assumptions`: list of concrete statements
- `conflicting_evidence`: list of `{fact, sources, chosen, why}`
- `checks`: `{passed, failed}`
- `decided_in_ms`, `decision_deadline_ms` (60000)
- `status`: `PUBLISHED` | `NO_ACTION` | `REJECTED` | `FAILED_TIMEOUT` | `FAILED_BUDGET`

CLI/API for a single decision prints **only** that object. Replay writes JSONL, one object per trigger (including duplicates after the first: idempotent replay of stored decision, same `decision_id`).

## Decide path (per trigger)

1. If `event_id` already processed → return stored decision (idempotent).
2. Record processing start (monotonic + wall clock).
3. If holdout → `NO_ACTION`, persist, return.
4. Ingest trigger into facts (idempotent).
5. Reconcile viewer snapshot at `deliver_at`.
6. Select goal deterministically (or null).
7. Retrieve candidate titles/policy rows with IDs (tools counted).
8. Optional LLM propose **from those IDs only**; mock faults applied; skip LLM if budget, latency, or 503 window.
9. Planner builds proposal (homepage must have ≥3 titles/row before verify).
10. Verifier runs full gate. Failure → downgrade to safe homepage subset or `NO_ACTION`, never force-publish.
11. Persist decision, optional action row, plan. Stop if `decided_in_ms` would exceed 60s: `FAILED_TIMEOUT` / `NO_ACTION`.

No action file row for `NO_ACTION` (README). Decision still required.

## Verifier gate (independent)

Must be a separate function. Inputs: proposal + reconciled facts + catalogue + policy + rule_version. Checks include:

- holdout
- event_id idempotency
- goal eligibility vs facts (not vs LLM)
- success signal exists in schema
- dialect in d1–d6 and matches app feed
- each title_id exists, dialect match, available, not withdrawn
- unique content_slug on page; row length; hero; TTL fields present
- consent, quiet hours, frequency, templates, pricing, forbidden phrases
- copy/titles not treating description as instructions
- tool/model budget
- latency budget still remaining

Generated text cannot pass a check.

## State (SQLite tables, planned)

- `processed_events(event_id PRIMARY KEY, first_deliver_at, decision_id)`
- `facts(viewer_id, fact_key, value_json, source, source_id, at, as_of)`
- `profile_rows` raw duplicates retained
- `active_plans(viewer_id PRIMARY KEY, goal_id, deadline, success_signal, rule_version, started_at)`
- `decisions` full JSON + columns for query
- `actions` simulator-shaped JSON
- `outcomes(viewer_id, goal_id, started_at, status, signal_event_id, judged_at)`
- `lessons(viewer_id, at, text, evidence_ids)`
- `rule_versions(goal_id, version, spec_json, effective_at)`
- `control_log`

Do not rewrite all facts on a rule change. G03 correction updates `rule_versions` and rows in `active_plans` where `goal_id=G03`.

## Retrieval (RAG, proportional)

No vector database in v1. Catalogue loaded into SQLite.

- Structured retrieve: `dialect = ? AND available at T AND NOT withdrawn`
- Filters: genre, content_type, release_date for G08, slug for G07
- FTS5 on `title` + `description` only to match a viewer `search.query`; hits still pass structured eligibility

Every retrieved item keeps `title_id`. Empty retrieve → no content action.

## LLM

Default `OU_LLM=mock`. Mock:

- returns a proposal among retrieved IDs or abstain
- on `model_faults.jsonl` match: exact `model_output` or sleep `seconds` then timeout
- during control 503 window: raise unavailable
- never receives full `events.jsonl`

Hosted providers are opt-in via env and must not be sent the pack unless explicitly configured (privacy). Tests always use mock.

## Measurement

After `simulator/simulate_outcomes.py`:

- Judge each committed goal with **that goal’s rule_version**: required event in window, condition, attributable to post-commit signals (simulator + live events). Inconclusive if signal missing because the goal was unmeasurable (should not have been committed).
- Impact: ITT `agent` vs `holdout` among triggered users. Report selection-bias of “served vs unserved” using pilot math as a warning, not as the score.
- Do not fabricate lift numbers.

## Frontend

Last. Calls `DecisionService.decide` for a real `user_id`. Displays reconciled facts, conflicts, decision JSON, checks. No parallel scorer.

## Explicit non-designs

- Per-viewer hardcoded maps
- Recommender-only ranked lists without a goal
- Microservices, Redis, Kafka
- Embedding eligibility
- Treating simulator `policy_problems` as a substitute for our verifier (we must block before publish)
