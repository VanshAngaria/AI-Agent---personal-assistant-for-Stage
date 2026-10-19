# One User, One Agent — submission (foundation)

This directory is the deliverable. The private synthetic data pack stays **outside** this tree. Point the process at it later with `OUA_PACK_PATH`.

## Status of this stage

Implemented: typed decision schema, SQLite state, deterministic goal selection, catalogue/policy retrieval, homepage row assembly.

**Not implemented:** event ingestion, reconciliation, verification, mock LLM, replay CLI, simulator wiring, impact reports, Streamlit.

Do not treat this package as a working agent.

## Setup

Python 3.11+.

```powershell
cd submission
python -m pip install -e ".[dev]"
python -m pytest
```

## Configuration

| Variable | Meaning |
|---|---|
| `OUA_PACK_PATH` | Absolute path to the local data pack root (not required for foundation tests) |
| `OUA_DB_PATH` | SQLite file (default `viewer_state/agent.sqlite`) |
| `OUA_GOALS_PATH` | Goal catalog JSON (default `config/goals.json`) |

`config/goals.json` is a copy of the pack’s **rule catalog**, not viewer events. Viewer/title evidence must still be read from `OUA_PACK_PATH` when ingestion exists.

## Layout

```
submission/
  src/oua/          package
  tests/            foundation tests
  config/           versioned rules (goals, interventions, pricing, messaging policy)
  viewer_state/     runtime SQLite (empty until a run)
  sample_run/       replay artifacts (not produced yet)
```

## Decision JSON

One Pydantic model (`oua.schemas.Decision`, `schema_version` `1.0.0`). Statuses:

- `PUBLISHED` — publishable action, measurable goal, independent checks passed, executor/simulator `execution.confirmed=true`
- `ABSTAINED` — `NO_ACTION` plus a non-empty `abstention_reason`
- `FAILED_SAFE` — `NO_ACTION` plus `failure_reason`; publication did not occur
