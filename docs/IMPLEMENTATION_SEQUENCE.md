# Implementation sequence

Do not generate the whole project in one operation. Each stage ends at a gate. Stop for review after Stage 0 (this reconnaissance). Later stages stop if a gate fails.

Preferred stack only: Python, Pydantic, SQLite, pytest, Streamlit (UI last).

## Stage 0 — Reconnaissance (current)

**Build:** documentation only.

**Gate:**

- [x] Brief read
- [x] Every pack file that exists was scanned or opened
- [x] Six docs written
- [x] No production code
- [ ] Human review of plan, especially G06/G07 interpretation, live `app_open` handling, in-app payload

**Exit:** explicit go-ahead.

## Stage 1 — Schema and durable state

**Build:** `src/oua/schemas.py`, `src/oua/state/db.py`, empty `DecisionService` that can persist and load.

**Tests:** `tests/test_decision_schema.py`, `tests/test_state_persistence.py`

**Assert:** required fields; invalid action enum rejected; SQLite round-trip of a synthetic decision that uses **placeholder structure** not a real viewer’s hardcoded outcome.

**Gate:** pytest stage 1 green; no pack facts invented in fixtures except IDs copied from fixtures loaded from pack slices.

## Stage 2 — Ingestion and idempotency

**Build:** loaders for events, profiles (keep duplicate rows), catalogue, episodes, arms, goals, interventions, pricing.

**Tests:** `tests/test_idempotency.py`, `tests/test_late_triggers.py`

**Assert:** same `event_id` two `received_at` values → one stored event, second is a redelivery; late trigger still ingested; `event_age_s` computed from `received_at - occurred_at` without treating occurred_at as truth.

**Gate:** assignment-required “late or duplicate trigger” test exists and would fail if duplicates created two rows.

## Stage 3 — Reconciliation and precedence

**Build:** `src/oua/reconciliation/` implementing `SOURCE_PRECEDENCE.md`.

**Tests:** `tests/test_conflicts.py`

**Assert:**

- Triple dialect mismatch keeps three sources; `dialect.used` from app feed
- Duplicate profile rows both visible; working row = max `updated_at`
- `notification_opt_out` overrides snapshot
- Missing profile ≠ `subscription_status=never`

**Gate:** assignment-required “conflicting source” test green.

## Stage 4 — Catalogue retrieval and policy as data

**Build:** SQLite catalogue + FTS optional; policy loader; injection text stored as description only.

**Tests:** `tests/test_retrieval.py`, `tests/test_prompt_injection.py`

**Assert:** retrieve by `title_id` returns that row; d7 retrieve empty; `T0001` after withdraw time excluded; `T0002` description does not appear as a tool “instruction”; FTS hit still dropped if wrong dialect.

**Gate:** no fabricated retrieval in tests.

## Stage 5 — Goal eligibility (deterministic)

**Build:** `goal_selector.py`, `rule_versions` seeded from `goals.json`.

**Tests:** `tests/test_goal_selector.py`

**Assert:** G10 and G11 never selected; G04 false for profile `never` even if a stub LLM says trial; G01 requires signup window + no `play_started`; one active goal slot.

**Gate:** unmeasurable goals cannot be committed.

## Stage 6 — Planner + independent verifier

**Build:** homepage/message/no-action proposals; `verifier.py` as the only publisher.

**Tests:** `tests/test_verifier.py`, `tests/test_homepage.py`, `tests/test_messaging_policy.py`, `tests/test_whatsapp_templates.py`, `tests/test_holdout.py`, `tests/test_copy_and_pricing.py`, `tests/test_abstention.py`

**Assert:** holdout cannot publish; quiet hours block send_at; frequency cap; WhatsApp unknown template fails; row with 2 titles fails; wrong dialect fails; verifier rejects planner proposal; NO_ACTION recorded with reason.

**Gate:** verifier can fail a deliberately wrong proposal in-process.

## Stage 7 — Mock LLM, budgets, control faults

**Build:** mock provider, tool counters, control windows.

**Tests:** `tests/test_model_faults.py`, `tests/test_budgets.py`, `tests/test_failures.py`, `tests/test_latency.py`

**Assert:** unsupported_claim does not set trial; malformed JSON → fallback; timeout 45s does not exceed 60s publish (cancel/fallback); 503 window skips model; >8 model or >20 tool calls fail closed; data-store timeout uses last reconciled snapshot or NO_ACTION.

**Gate:** assignment-required “tool failure” test green; mock mode needs no API key.

## Stage 8 — Replay harness

**Build:** merge triggers, live, control by time; CLI `python -m oua.replay --pack . --out sample_run`; JSONL decisions + actions.

**Tests:** `tests/test_replay_io.py` on a **tiny fixture** cut from pack (not 2769 hardcoded expected titles).

**Assert:** output is JSONL of valid Decision objects; duplicate trigger lines replay stored decision; `published_at` = deliver_at + processing; holdout has no actions file rows.

**Gate:** one command documented in README (project README, not mutating pack README contents table). Prefer `docs` + later `submission/README.md` rather than rewriting pack README.

## Stage 9 — Goal-rule correction and plan revision

**Build:** apply G03 correction to open goals only; purchase live event revises that viewer.

**Tests:** `tests/test_goal_correction.py`, `tests/test_plan_revision.py`

**Assert:** other viewers’ history unchanged; open G03 success signal becomes `play_started`; closed outcomes untouched; post-decision purchase can close G05/G06.

**Gate:** assignment-required “corrected goal rule” test green.

## Stage 10 — Outcomes and impact

**Build:** consume simulator output; ITT report; known-limitations of `in_app_message` simulation.

**Tests:** `tests/test_outcomes.py`, `tests/test_impact.py`

**Assert:** success requires configured event; G11 cannot succeed; primary impact table is agent vs holdout; a function computing served-vs-unserved exists only to show it is misleading (pilot numbers).

**Gate:** no invented lift; reports generated from files.

## Stage 11 — Confidence, evidence quality, audit query

**Build:** `confidence.py` heuristic documented in code comments and KNOWN_LIMITATIONS; audit fetch by viewer_id.

**Tests:** `tests/test_confidence.py`, `tests/test_evidence.py`, `tests/test_audit.py`, `tests/test_ownership.py`

**Assert:** confidence_reason mentions actual missing/conflict facts; evidence_ids resolve; audit returns facts known at timestamp T.

**Gate:** rubric Evidence 15% has test evidence.

## Stage 12 — Thin Streamlit + submission wrappers

**Build:** `frontend/app.py` calling `DecisionService`; `submission/` notes; `AI_COLLABORATION.md` only from this conversation’s real actions; requirements matrix regenerated by a script that parses `docs/EVALUATION_MATRIX.md`.

**Tests:** `tests/test_single_viewer_service.py` against real pack IDs **looked up dynamically** (e.g. first trigger user), not a baked decision.

**Gate:** `pytest` full suite; sample_run produced once by replay (long run, not required inside unit tests).

## What this sequence intentionally leaves later or incomplete

- Hosted LLM
- Vector RAG
- Perfect G07 series inference
- In-app copy design
- UI polish
- Calibrated probability (will not be claimed)

## Stage-gate checklist (copy for review)

1. Stage 0 docs accepted (G06/G07/live-open/in-app decisions recorded).
2. Schema+SQLite tests pass.
3. Idempotent late/duplicate ingestion tests pass.
4. Conflict/precedence tests pass.
5. Retrieval never fabricates IDs; injection ignored.
6. G10/G11 cannot be selected.
7. Verifier rejects bad proposals; holdout/consent/quiet/frequency/dialect hold.
8. Mock faults + budgets + 503 degrade safely under 60s.
9. Replay CLI JSONL + idempotent redelivery.
10. G03 correction scopes to open G03 only.
11. Outcomes attributable; impact is ITT holdout.
12. Submission artifacts and honest AI log; matrix counts scripted.
