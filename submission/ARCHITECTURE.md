# Architecture (foundation)

Matches `docs/ARCHITECTURE_PLAN.md` in the workspace, narrowed to what exists now.

## Package

`src/oua/` is the only backend. CLI, replay and a future Streamlit UI must import `DecisionService`. There is no second pipeline.

| Module | Now | Later |
|---|---|---|
| `schemas.py` | Decision, goal, evidence, checks, execution ack | unchanged contract |
| `goals.py` | Load `config/goals.json`; refuse unmeasurable signals | G03 version bumps from control events |
| `config.py` | Paths, latency and call budgets | pack ingestion root |
| `state/store.py` | SQLite + constraints | used by ingest/decide |
| `service.py` | persist/load + catalog check | `decide(trigger)` |

## Persistence constraints

- `processed_triggers.event_id` primary key — duplicate deliveries do not insert a second trigger
- `decisions.trigger_event_id` unique — one stored decision per trigger
- `active_plans.viewer_id` primary key — at most one active goal
- `actions`: unique `action_id`; unique `(decision_id, type)`; unique `(viewer_id, trigger_event_id, type)`
- `experiment_assignments.arm` check in `('agent','holdout')`
- `outcomes.status` check in `('succeeded','failed','inconclusive')`
- Facts are append-only with `source`, `source_id`, `at`

`set_active_plan(..., replace_existing=False)` raises if a plan already exists so two components cannot silently fork goals.

## Trust boundary

Deterministic schema and catalog checks are the only enforcement in this stage. An LLM cannot write rows except through code that does not exist yet. Generated text is not evidence.

## Data pack

Viewer events, profiles, catalogue and replay streams remain in the external pack. This package copies **goal/intervention/pricing/policy** files only, because they are configuration, not personal event data.
