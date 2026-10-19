# One User, One Agent

A per-viewer lifecycle agent for the OTT dialect platform. On every app open it:

1. assembles what is known about the viewer, with source and freshness for each fact;
2. commits to one measurable goal, or explains why it cannot;
3. retrieves the titles that match this viewer's own searches and viewing;
4. has a language model compose the home page and message copy, within those retrieved candidates only;
5. sends the result through an independent verifier and publishes within the latency budget.

Afterwards it judges every goal from attributable signals and measures impact against the randomized holdout.

**No API key is needed.** The default model provider is a local deterministic mock that also replays
`replay/model_faults.jsonl`. Claude is opt-in (`--llm anthropic`, see below).

For a guided explanation, read `docs/1_HIGH_LEVEL_DESIGN.md`, then `docs/2_LOW_LEVEL_DESIGN.md`, then
`docs/3_FILE_GUIDE.md`.

## Setup

Python 3.11+ (developed on 3.14). The only runtime dependency is `pydantic`.

```
cd submission
python -m pip install -e ".[dev]"          # add ,llm for Claude
```

The data pack is expected one directory up (this repository's root). Point elsewhere with `--pack PATH`.

## One command: replay, simulate, judge, report

```
python -m oua run-all
```

This runs four steps (each is also its own command) and writes everything to `sample_run/` in about 40 seconds:

| Step | Command | Output |
|---|---|---|
| Replay the trigger script through the agent (fresh state) | `python -m oua replay` | `decisions.jsonl`, `actions.jsonl`, `latency_report.json`, `replay_summary.json` |
| Run the pack's outcome simulator on the published actions | `python -m oua simulate --seed oua-practice` | `sim/outcome_events.jsonl`, `sim/simulator_report.json` |
| Judge every goal (both arms) and write lessons | `python -m oua judge` | `outcomes.jsonl` |
| Impact vs holdout, viewer state export, final report | `python -m oua impact` | `impact_report.md`, `viewer_state/*.json`, `final_report.md` |

Use another simulator seed with `python -m oua run-all --seed my-seed`. Durable state lives in
`viewer_state/agent.sqlite` (SQLite; `--db` to move it).

Check the submission against the brief (every section 8 requirement, every section 9 scoring area and every "will
score poorly" item, each with evidence from the generated files). This writes `sample_run/evaluation_check.md`:

```
python -m oua check --run-tests
```

Audit any viewer at any moment, showing what the agent knew, decided and observed:

```
python -m oua audit --viewer U-01991 --at 2026-10-01T12:00:00Z
```

## Running with Claude (optional)

```
python -m pip install -e ".[llm]"
export ANTHROPIC_API_KEY=...            # or `ant auth login`
python -m oua run-all --llm anthropic   # default model claude-opus-5-5 at effort "low"; --llm-model to change
```

The pack's injected model faults are still applied on top of the real provider.

**Privacy.** Claude receives derived features (status, counts, dialect, top genres), candidate title IDs with names,
genres and a capped summary, and search terms. It never receives raw event or profile rows, viewer IDs or timestamps.
Treat a Claude run as sending pack-derived data to Anthropic's API, and only do it where that is allowed.

**Latency.** Model calls run inside the 3 s page target. A call that would overrun it is skipped, as are all model
calls when the queue backs up. Each skip falls back to the deterministic path and is recorded in the decision.

## Where behaviour is configured

Nothing about goals, thresholds or copy is hard-coded:

| File | What it holds |
|---|---|
| `../config/goals.json` (pack) | The 12 goals: structured eligibility and success signals. Corrections arrive as control events and become new rule versions |
| `config/agent_policy.json` | Business policy, versioned and cited by every decision. It holds: <ul><li>readings of the vague goal rules</li><li>the success matchers</li><li>goal priority</li><li>stale-trigger threshold</li><li>per-goal intervention strategies and row sources</li><li>follow-up rules and message templates</li><li>injection patterns</li><li>retrieval weights</li><li>which model tasks are on</li><li>confidence weights</li></ul> |
| `config/agent.json` | Runtime settings: latency and call budgets, timeouts, workers, load-shedding threshold, model provider |

A new goal that uses existing checks and matchers needs config only. A goal whose eligibility is free text with no
reviewed interpretation is refused, never guessed. A bad policy edit (unknown check, matcher or row source) fails at
start-up.

## Tests

```
python -m pytest
```

There are 75 tests. They run on a small synthetic fixture pack (generated viewers and titles, with the real rule configs),
so they don't need the private data. `tests/test_real_pack.py` adds four checks that run only when the pack is present.

| Required by the brief | Tests |
|---|---|
| Late or duplicate trigger | `test_idempotency.py`: <ul><li>duplicate delivery, buffered offline open, device clock offset</li><li>history and live redelivery, same-session reopen</li><li>crash between claim and commit</li></ul> |
| Conflicting source | `test_conflicts.py`: three dialects, duplicate profile rows, purchase vs stale "never", opt-out vs snapshot, missing profile |
| Tool failure | `test_failures.py`: <ul><li>model 503 with circuit breaker</li><li>45 s model timeout (no second wait)</li><li>malformed output</li><li>data store down, with and without a snapshot</li><li>budget exhaustion</li></ul> |
| Corrected goal rule | `test_goal_correction.py`: <ul><li>only open G03 plans change; closed outcomes untouched</li><li>missing `window_hours` inherited</li><li>new goals and judging use the new rule</li><li>the correction survives a restart</li></ul> |

Other tests cover:
- **Model and RAG** (`test_rag_llm.py`): retrieval cites the viewer's own events, never crosses dialect or quarantine,
  and skips what the viewer already watched. Invented IDs from the model are removed. An ungrounded composition falls
  back to the rules. The model can decline a message but never add one. Policy-breaking copy is replaced. Prompts carry
  no raw rows. A queue backlog sheds model calls.
- **Claude provider** (`test_anthropic_provider.py`): the real Anthropic SDK against a local fake API, covering the
  request shape, structured output, server fallbacks, errors, timeout and refusal.
- **Policy** (`test_policy.py`): config-only new goals, refusal of uninterpreted goals, and changing behaviour by config.
- **Safety, measurement and harness.**

Twenty-one key behaviours were broken on purpose to check the suite catches each one; see `AI_COLLABORATION.md`.

## Reading a decision

Each line of `sample_run/decisions.jsonl` follows the brief's section 4 structure, extended. Key fields:

* `goal`: id, deadline, success signal with `judged_by`, rule version, plan id, and whether the plan was kept from an earlier decision.
* `action` / `actions`: what was published.
* `no_action_reason` / `no_action_kind`: why nothing was published (holdout, stale_trigger, no_goal, no_dialect, page_already_live, verifier_blocked, data_store, budget).
* `dialect`: the dialect used, its source and the conflicting values.
* `evidence` plus `evidence_details`: every cited id resolves to a source, timestamp and freshness. `retrieval:T…` entries carry the matched terms and the viewer events they came from.
* `retrieval`: query terms, how many searches and plays fed the query, and the top hits.
* `llm`: provider and model, plus each task (choose_goal, compose_page, write_copy) with its outcome. `model` is the goal-choice task on its own.
* `composition`: who composed the page (the model or the rules), the model's rationale, and anything the grounding check removed.
* `confidence` plus `confidence_reason`: a heuristic score with each term listed. Weights live in the policy.
* `assumptions`, `conflicting_evidence`, `goal_evaluations` (each of the 12 goals, with the checks run), `rationale`.
* `checks`: passed/failed checks from the independent verifier.
* `budget`: tool and model calls with a per-call log.
* `policy_version`, plus timing: `decided_in_ms`, `queue_wait_ms`, `latency_ms`, `published_at`, `status`.

## Layout

```
submission/
  README.md  ARCHITECTURE.md  AI_COLLABORATION.md  KNOWN_LIMITATIONS.md
  docs/                    1_HIGH_LEVEL_DESIGN.md, 2_LOW_LEVEL_DESIGN.md, 3_FILE_GUIDE.md (explainers)
  config/agent_policy.json versioned business policy (cited by every decision)
  config/agent.json        runtime settings (budgets, timeouts, provider)
  src/oua/
    agent.py               the per-viewer agent: decide pipeline, live events, control events
    reconcile.py           facts with provenance, per-fact precedence, conflicts
    policy.py              loads and validates agent_policy.json
    goals.py               rule versions, generic eligibility checks, selection, success matchers
    retrieval.py           BM25 over the catalogue, queried with the viewer's searches and plays (the R in RAG)
    planner.py             candidate rows, model payload, grounding checks, follow-ups
    llm.py                 prompts, JSON schemas, mock and Claude providers, fault replay, advisor
    verifier.py            independent gate (re-derives from raw rows); only path to publication
    dependencies.py        budgeted tool/model client, deadlines, load shedding, fault windows, circuit breaker
    catalogue.py           availability, withdrawal, popularity evidence, policy-driven quarantine
    store.py               SQLite state with schema-enforced invariants
    harness.py             replay in delivery order, worker-pool queue model, latency report
    measure.py             judge, lessons, holdout impact, viewer-state export, audit, final report
    schemas.py             decision record and its invariants
    check.py               evaluation check against the brief (`python -m oua check`)
    cli.py                 command line
  tests/                   75 tests + fixture configs
  sample_run/              outputs of `python -m oua run-all`
  viewer_state/            SQLite state (generated)
```
