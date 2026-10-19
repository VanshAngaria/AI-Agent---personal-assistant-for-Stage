# One User, One Agent

**A per-viewer AI lifecycle agent for a regional-dialect OTT platform.** On every app open it decides one measurable
goal for that viewer and one proportionate action (a personalised home page, one follow-up message, or nothing). It
cites evidence for every step, stays within a strict latency budget, and afterwards measures honestly, against a
randomized holdout, whether it helped.

Built for the *One User, One Agent* Agentic AI Engineering assignment.

---

## What it does

```
App open ─► understand the viewer ─► pick ONE goal ─► retrieve content ─► model composes ─► independent verifier ─► publish
             facts + source + age     12 goals,         BM25 over the       within retrieved    23+ checks; holdout,     within
             conflicts kept visible   rules decide      viewer's searches   candidates only     dialect, consent, caps,  ~1 s
                                      eligibility       and plays           (grounded)          quiet hours, injection
                                   … 14 days later …
         ─► judge each goal from attributable signals ─► store a lesson for the viewer ─► impact vs randomized holdout (ITT)
```

* **Code decides what is allowed; the model judges within it.** Eligibility, consent, dialect and availability come
  from data and versioned config. The model (Claude, or a deterministic local mock) only chooses among eligible goals,
  composes the page from retrieved candidates, and writes copy. Its output is grounding-checked and verified.
* **Evidence or nothing.** Every decision cites events, profile rows, catalogue rows, rule versions and the policy
  version, each with its age. Model text is never evidence.
* **Do nothing when that's right.** Holdout, stale (buffered) opens, no eligible goal, a dialect with no content, and a
  page that's already live are all recorded decisions with reasons.
* **Behaviour lives in config, not code.** Goal rules come from `goals.json` plus a versioned policy file.
* **Real-time and fail-safe.**
  * Idempotent on duplicate triggers.
  * Deadline-aware model calls and load shedding.
  * Circuit breaker on model failures.
  * A cached snapshot when the data store is degraded, and fail-closed when the agent can't act safely.
* **Honest measurement.** Intent-to-treat on randomized arms, with "shadow goals" for the holdout. Served-vs-unserved
  comparisons are shown only to explain why they mislead.

## Results of the sample run

| | |
|---|---|
| Decisions | 2,687 (90 duplicate deliveries answered idempotently) |
| Actions | 920 home pages, 223 push, 11 WhatsApp |
| Policy problems found by the simulator | **0** |
| Latency | p50 0.44 s · p95 1.25 s · max 6.9 s · **0** over the 60 s budget |
| Surprise events handled | buffered opens, three-way dialect conflicts, mid-run rule correction, title withdrawal, model false claim / malformed output / timeout, model and data-store outages |
| Impact (ITT, first goal) | agent 13.3% vs holdout 12.3%, **+1.0 pt (95% CI −2.4 to +4.4)**: no detectable effect, reported as such |
| Evaluation check against the brief | **48 / 48 pass** (`submission/sample_run/evaluation_check.md`) |
| Tests | **75 passing** |

## Quick start

**Requirements:** Python 3.11+. The only runtime dependency is `pydantic`.

**1. Add the data pack.** The pack's viewer data is personal data and is **not** in this repository. Copy its `data/`,
`replay/` and `pilot/` folders into the repository root, next to `config/` and `simulator/`.

**2. Install and run:**

```bash
cd submission
python -m pip install -e ".[dev]"

python -m oua run-all            # replay → simulator → judge → reports   (~40 s, writes sample_run/)
python -m oua check --run-tests  # verify everything against the brief  (48 checks + the 75 tests)
python -m pytest                 # tests alone (run without the pack; 4 pack-only tests skip)
```

**3. Look at the results** in `submission/sample_run/`:

| File | What it shows |
|---|---|
| `final_report.md` | Start here: what happened, latency, every surprise with example decision IDs, outcomes, impact |
| `impact_report.md` | Agent vs holdout, per-goal effects, and why the pilot's "2.3× uplift" was misleading |
| `evaluation_check.md` | Every requirement of the brief, checked against the outputs |
| `decisions.jsonl` | Every decision: goal, action or reason for none, evidence with freshness, checks, model tasks, timing |
| `viewer_state/U-xxxxx.json` | One viewer's full durable state |

Follow one viewer through time (what the agent knew, decided and observed):

```bash
python -m oua audit --viewer U-02344
python -m oua audit --viewer U-02344 --at 2026-10-01T06:00:00Z
```

**Optional: use Claude instead of the mock.** No key is needed otherwise.

```bash
python -m pip install -e ".[llm]"
export ANTHROPIC_API_KEY=...          # PowerShell: $env:ANTHROPIC_API_KEY = "..."
python -m oua run-all --llm anthropic --out ../claude_run
```

This sends derived features and candidate titles (never raw rows) to Anthropic's API. Use it only where that's
allowed.

## Documentation

| Document | For |
|---|---|
| [`submission/docs/1_HIGH_LEVEL_DESIGN.md`](submission/docs/1_HIGH_LEVEL_DESIGN.md) ([PDF](submission/docs/pdf/1_HIGH_LEVEL_DESIGN.pdf)) | The why: principles, components, decision life cycle, trade-offs, interview answers |
| [`submission/docs/2_LOW_LEVEL_DESIGN.md`](submission/docs/2_LOW_LEVEL_DESIGN.md) ([PDF](submission/docs/pdf/2_LOW_LEVEL_DESIGN.pdf)) | The how: tech stack, data flow, step-by-step pipeline, algorithms, data model |
| [`submission/docs/3_FILE_GUIDE.md`](submission/docs/3_FILE_GUIDE.md) ([PDF](submission/docs/pdf/3_FILE_GUIDE.pdf)) | Every file and folder, plus a 10-minute walkthrough |
| [`submission/README.md`](submission/README.md) | Detailed usage, configuration, tests, how to read a decision |
| [`submission/ARCHITECTURE.md`](submission/ARCHITECTURE.md) | Architecture note: ownership, planning, memory, verification, latency, failure recovery, trust boundaries |
| [`submission/AI_COLLABORATION.md`](submission/AI_COLLABORATION.md) | How AI tools were directed, checked and sometimes overruled |
| [`submission/KNOWN_LIMITATIONS.md`](submission/KNOWN_LIMITATIONS.md) | What is intentionally incomplete |
| [`docs/DATA_PACK_README.md`](docs/DATA_PACK_README.md) | The supplied data pack's formats and conventions |

## Repository layout

```
├── README.md                 this file
├── config/                   platform rules supplied with the data pack (goals, interventions, pricing, policy)
├── simulator/                supplied outcome simulator (unmodified)
├── docs/                     data-pack guide + reconnaissance notes written before the build
├── data/ replay/ pilot/      the data pack (NOT committed; add locally)
└── submission/               the deliverable
    ├── README.md  ARCHITECTURE.md  AI_COLLABORATION.md  KNOWN_LIMITATIONS.md
    ├── config/               agent_policy.json (versioned business policy) · agent.json (runtime settings)
    ├── docs/                 explainer documents (+ PDFs)
    ├── src/oua/              the agent (Python package)
    ├── tests/                75 tests on a synthetic fixture pack
    └── sample_run/           decisions, actions, outcomes, latency & impact reports, per-viewer state, evaluation check
```

## Tech stack

Python · Pydantic v2 · SQLite · BM25 retrieval (pure Python) · Anthropic SDK with Claude (optional) · pytest

## A note on data

The viewer data in the pack is synthetic but handled as personal data, as the brief requires:

* The raw pack (`data/`, `replay/`, `pilot/`) is git-ignored and not published.
* The required sample run in `submission/sample_run/` contains the agent's outputs for that synthetic data (viewer IDs,
  decisions, per-viewer state).
* By default no model provider receives any of it; the mock runs locally.
