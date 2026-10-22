# One User, One Agent (OUA): Complete Project Overview & Guide

## 1. Executive Summary
**One User, One Agent (OUA)** is an autonomous, per-viewer lifecycle AI agent built for a regional-dialect OTT video streaming service (supporting dialects d1–d6 in India). 

On every mobile app open, the agent operates in real-time to:
1. Understand the viewer's history and reconcile personal facts.
2. Select **one single measurable goal** (from 12 structured goals, such as first episode conversion, series re-engagement, or subscription renewal).
3. Retrieve personalized catalogue recommendations using lexical search.
4. Compose a proportionate action (a personalized home page and/or a scheduled follow-up message).
5. Enforce zero-trust safety and business policy checks through an independent verifier.
6. Commit actions atomically and later evaluate causal uplift against a randomized holdout arm using **Intent-to-Treat (ITT)** principles.

---

## 2. How the Project Runs

### 2.1 System Requirements
- **Python 3.11+**
- **Data Pack**: Synthetic data folders (`data/`, `replay/`, `pilot/`) placed in the repository root directory alongside `config/` and `simulator/`.
- **Runtime Dependencies**: Only `pydantic` (v2) is strictly required for the core agent. No external database server, cache server, or message queue is required.

### 2.2 Installation
From the root workspace directory:
```bash
cd submission
python -m pip install -e ".[dev]"
```

### 2.3 CLI Commands

| Command | Description | What Happens Under the Hood |
|---|---|---|
| `python -m oua run-all` | **Full End-to-End Pipeline** | 1. Ingests data pack into SQLite.<br>2. Replays triggers & events with virtual clock.<br>3. Runs the standalone simulator (`simulator/simulate_outcomes.py`).<br>4. Judges outcomes & attribution.<br>5. Generates reports in `submission/sample_run/`. |
| `python -m oua check --run-tests` | **Compliance & Test Verification** | Runs 48 evaluation checks against the project brief and executes all 75 automated tests. |
| `python -m pytest` | **Unit & Integration Tests** | Executes 75 pytest tests (mock provider, verifier, schemas, goal engine). |
| `python -m oua audit --viewer U-02344` | **Single-Viewer Audit** | Traces the full timeline for a viewer: events, facts, decisions, plans, and outcomes. |
| `python -m oua audit --viewer U-02344 --at 2026-10-01T06:00:00Z` | **Point-in-Time Audit** | Reconstructs what the agent knew, evaluated, and decided at a specific moment in time. |

### 2.4 Deterministic Mock vs. Anthropic Claude
- **Default (Offline Mock)**: Uses `MockProvider` ([oua/llm.py](file:///c:/Users/vansh/Downloads/one_user_one_agent_data_pack%20(4)/one_user_one_agent_pack/submission/src/oua/llm.py)). Requires no internet connection, zero API keys, and mirrors policy rules deterministically while simulating faults.
- **Anthropic Claude Mode**:
  ```bash
  python -m pip install -e ".[llm]"
  $env:ANTHROPIC_API_KEY = "your-api-key"   # PowerShell
  python -m oua run-all --llm anthropic --out ../claude_run
  ```
  Uses `claude-opus-5-5` with JSON Schema structured outputs, ephemeral system prompt caching, and server-side fallback.

---

## 3. End-to-End Workflow

```
 [Raw Pack Files] ──► [pack.py: Load & Ingest] ──► [SQLite DB: store.py]
                                                          │
 ┌────────────────────────────────────────────────────────┘
 ▼
[Replay Timeline: harness.py] 
    ├─► Control Events (title withdrawn, rule corrected, fault window)
    ├─► Live Events (background watch/subscription events -> closes open plans)
    └─► App Open Triggers ──► [AgentService.handle_trigger in agent.py]
                                   │
                                   ├─ 0. Idempotency & A/B Arm check
                                   ├─ 1. Tool reads + Reconcile facts & Stale check
                                   ├─ 2. Existing plan lifecycle check
                                   ├─ 3. Evaluate 12 goals (Rules) + LLM chooses
                                   ├─ 4. Holdout gating & Dialect check
                                   ├─ 5. BM25 Search + LLM compose page & push copy
                                   ├─ 6. Independent Verifier (23+ checks)
                                   ├─ 7. Finalise decision record
                                   └─ 8. Atomic Commit to SQLite
                                          │
                                          ▼
                      [decisions.jsonl] & [actions.jsonl]
                                          │
                                          ▼
                         [simulator/simulate_outcomes.py]
                                          │
                                          ▼
                   [measure.py: Attribution & ITT Analysis]
                                          │
                                          ▼
     [Final Reports: final_report.md, impact_report.md, viewer_state/]
```

### 3.1 The 8-Step Decision Pipeline ([AgentService.handle_trigger](file:///c:/Users/vansh/Downloads/one_user_one_agent_data_pack%20(4)/one_user_one_agent_pack/submission/src/oua/agent.py))
When an `app_open` event arrives:

1. **Step 0: Idempotency & Arm Assignment**:
   - Checks `processed_triggers`. Redeliveries return the previously committed decision.
   - Identifies if the viewer belongs to `agent` or `holdout` control arm.
2. **Step 1: Read Data & Fact Reconciliation ([reconcile.py](file:///c:/Users/vansh/Downloads/one_user_one_agent_data_pack%20(4)/one_user_one_agent_pack/submission/src/oua/reconcile.py))**:
   - Queries raw events and profile snapshots via the budgeted `ToolClient`.
   - Computes reconciled facts with freshness timestamps and surfaces discrepancies.
   - Detects stale triggers (`sent_at - occurred_at > 600s`), exiting with `NO_ACTION stale_trigger`.
3. **Step 2: Plan Upkeep**:
   - Checks existing open plan. If its window elapsed or preconditions became invalid, closes or supersedes the plan.
4. **Step 3: Goal Evaluation ([goals.py](file:///c:/Users/vansh/Downloads/one_user_one_agent_data_pack%20(4)/one_user_one_agent_pack/submission/src/oua/goals.py))**:
   - Code executes structured eligibility checks against all 12 goals.
   - If multiple goals qualify, the AI model chooses the most appropriate goal with an explicit reason.
5. **Step 4: Holdout & Dialect Gating**:
   - If in the `holdout` arm, records a shadow goal and exits with `NO_ACTION holdout`.
   - If dialect is invalid or unsupported (`null`, `NA`, `d7`), exits with `NO_ACTION no_dialect`.
6. **Step 5: Content Retrieval & Composition ([retrieval.py](file:///c:/Users/vansh/Downloads/one_user_one_agent_data_pack%20(4)/one_user_one_agent_pack/submission/src/oua/retrieval.py), [planner.py](file:///c:/Users/vansh/Downloads/one_user_one_agent_data_pack%20(4)/one_user_one_agent_pack/submission/src/oua/planner.py))**:
   - Runs BM25 retrieval over catalogue metadata using search history and recent watch tokens.
   - Gathers candidate rows (Next Episode, Retrieved, Popular, New Releases).
   - Model composes hero banner and content rows.
   - **Grounding verification**: Strips out hallucinated content IDs not in the candidate pool.
   - Prepares scheduled follow-up notifications if permitted by policy.
7. **Step 6: Independent Verification ([verifier.py](file:///c:/Users/vansh/Downloads/one_user_one_agent_data_pack%20(4)/one_user_one_agent_pack/submission/src/oua/verifier.py))**:
   - Re-derives eligibility from raw records. Runs 23+ checks (quiet hours, dialect match, active title availability, pricing validation, copy length).
   - Automatically repairs minor flaws (drops bad titles) or blocks unsafe actions.
8. **Step 7 & 8: Finalisation & Atomic Storage ([store.py](file:///c:/Users/vansh/Downloads/one_user_one_agent_data_pack%20(4)/one_user_one_agent_pack/submission/src/oua/store.py))**:
   - Validates decision object against Pydantic schema.
   - Persists decision, facts, plan state, and actions in a single SQLite transaction.

### 3.2 Out-of-Band & Dynamic Events
- **Live Events (`on_live_event`)**: Background plays or subscription conversions. If a success condition is met, the active plan closes immediately as `succeeded` and writes a positive lesson.
- **Control Events (`on_control`)**:
  - `title_withdrawn`: Marks title unavailable and instantly republishes any live home pages containing it.
  - `goal_rule_corrected`: Updates success criteria on open plans without rewriting history.
  - `dependency_unavailable`: Activates fault-injection and circuit-breaker windows.

---

## 4. System Architecture

The project adheres to the core axiom: **"Code decides what is allowed; the model judges within it."**

```
┌────────────────────────────────────────────────────────────────────────┐
│                          ORCHESTRATION LAYER                           │
│  oua/harness.py: Virtual Clock, Worker Pool (Min-Heap), Viewer Locks   │
└──────────────────────────────────┬─────────────────────────────────────┘
                                   │
                                   ▼
┌────────────────────────────────────────────────────────────────────────┐
│                         CORE AGENT CONTROLLER                          │
│  oua/agent.py (AgentService): State Machine, Budgeting, Circuit Breaker│
├──────────────────────────────────┬─────────────────────────────────────┤
│  RECONCILIATION ENGINE           │  GOAL ENGINE & POLICY               │
│  oua/reconcile.py: Precedence,   │  oua/goals.py, oua/policy.py:       │
│  Freshness, Conflict Tracking    │  Deterministic eligibility checks   │
├──────────────────────────────────┼─────────────────────────────────────┤
│  RETRIEVAL ENGINE                │  LLM ADVISOR (BOUNDED TASKS)        │
│  oua/retrieval.py: Pure Python   │  oua/llm.py: Anthropic / Mock       │
│  BM25 Search over Catalogue      │  JSON schemas, Grounding validator  │
└──────────────────────────────────┴─────────────────────────────────────┘
                                   │
                                   ▼
┌────────────────────────────────────────────────────────────────────────┐
│                     INDEPENDENT VERIFIER (GATEWAY)                     │
│  oua/verifier.py: Zero-trust, reads raw tables, 23+ hard safety checks │
└──────────────────────────────────┬─────────────────────────────────────┘
                                   │
                                   ▼
┌────────────────────────────────────────────────────────────────────────┐
│                        STORAGE & DURABLE MEMORY                        │
│  oua/store.py: SQLite (WAL mode, partial unique indexes, transactions) │
└────────────────────────────────────────────────────────────────────────┘
```

### Key Architectural Tenets
1. **Per-Viewer Concurrency**: The harness processes multiple viewer timelines concurrently, but enforces a strict per-viewer lock. A single viewer can never have two decisions running simultaneously.
2. **One Plan Per Viewer**: Enforced at the database level by SQLite partial unique index (`ux_open_plan`).
3. **Zero-Trust Verification**: The verifier operates as an isolated firewall. It ignores reconciler assumptions and model output, re-deriving validity straight from raw tables.
4. **Virtual Time & Budgeting**: Virtual clock simulates real-world latency (20ms DB read, 400ms mock LLM). Target response latency is $< 3$ seconds to ensure home pages land before the user reaches the app home screen.

---

## 5. Technology Stack

| Component | Technology | Role in Project | Source Location |
|---|---|---|---|
| **Core Runtime** | Python 3.11+ | Implementation of all pipelines, CLI, and business rules | Entire codebase |
| **Data Schema Validation** | Pydantic v2 | Validating decision records, JSON schemas, and configuration models | [oua/schemas.py](file:///c:/Users/vansh/Downloads/one_user_one_agent_data_pack%20(4)/one_user_one_agent_pack/submission/src/oua/schemas.py) |
| **Database & Durable Memory** | SQLite3 (WAL mode) | Local embedded store with atomic transactions and partial unique indexes | [oua/store.py](file:///c:/Users/vansh/Downloads/one_user_one_agent_data_pack%20(4)/one_user_one_agent_pack/submission/src/oua/store.py) |
| **Search & Information Retrieval** | Custom BM25 (Pure Python) | Inverted index & relevance scoring over titles, genres, and synopsis | [oua/retrieval.py](file:///c:/Users/vansh/Downloads/one_user_one_agent_data_pack%20(4)/one_user_one_agent_pack/submission/src/oua/retrieval.py) |
| **LLM Inference** | Anthropic Python SDK (`claude-opus-5-5`) | Bounded reasoning: goal selection, layout composition, copywriting | [oua/llm.py](file:///c:/Users/vansh/Downloads/one_user_one_agent_data_pack%20(4)/one_user_one_agent_pack/submission/src/oua/llm.py) |
| **Offline Simulation** | MockProvider | Deterministic offline model emulator with fault injection | [oua/llm.py](file:///c:/Users/vansh/Downloads/one_user_one_agent_data_pack%20(4)/one_user_one_agent_pack/submission/src/oua/llm.py) |
| **OTT Environment Simulator** | Standalone Python script | Simulates viewer watching, engagement, churn, and click behavior | [simulator/simulate_outcomes.py](file:///c:/Users/vansh/Downloads/one_user_one_agent_data_pack%20(4)/one_user_one_agent_pack/simulator/simulate_outcomes.py) |
| **Testing Framework** | pytest | 75 unit/integration test cases | [submission/tests/](file:///c:/Users/vansh/Downloads/one_user_one_agent_data_pack%20(4)/one_user_one_agent_pack/submission/tests) |

---

## 6. Data Flow & Subsystems

### 6.1 Database Retrieval & Persistence ([store.py](file:///c:/Users/vansh/Downloads/one_user_one_agent_data_pack%20(4)/one_user_one_agent_pack/submission/src/oua/store.py))
SQLite stores durable state across 10 tables:
- `events`: All received events, deduplicated by `event_id`.
- `profile_rows`: Raw user profile snapshots from the warehouse.
- `facts`: Reconciled viewer facts (status, signup date, dialect) with freshness metadata.
- `decisions`: Complete decision audit trail.
- `actions`: Home page and message actions dispatched to the simulator.
- `plans`: Active and historical viewer goals.
- `plan_history`: Audit trail for all goal updates, cancellations, or completions.
- `outcomes`: Evaluation results (succeeded, failed, inconclusive).
- `lessons`: Long-term memory store (e.g., cooling down a goal that previously failed for this viewer).
- `rule_versions` & `control_log`: Tracks dynamic policy shifts, withdrawals, and fault windows.

### 6.2 Precedence & Conflict Resolution ([reconcile.py](file:///c:/Users/vansh/Downloads/one_user_one_agent_data_pack%20(4)/one_user_one_agent_pack/submission/src/oua/reconcile.py))
- **Dialect**: App setting at trigger (`app_dialect`) is king. Disagreements with profile are preserved as conflicts.
- **Subscription**: Billing events occurring after the profile's `updated_at` override profile subscription values.
- **Opt-Outs**: Any recorded `notification_opt_out` permanently revokes push/WhatsApp communication for that channel.

### 6.3 Lexical Retrieval Engine ([retrieval.py](file:///c:/Users/vansh/Downloads/one_user_one_agent_data_pack%20(4)/one_user_one_agent_pack/submission/src/oua/retrieval.py))
- **Field Boosts**: Title names (3.0), Genres (2.0), Description (1.0).
- **BM25 Parameters**: $k_1 = 1.2$, $b = 0.75$.
- **Queries**: Composed from viewer search terms (2.0 weight) and played titles (1.0 weight) over the past 30 days.
- **Filtering**: Enforces active dialect match, current catalogue availability, removes quarantined content, and filters out already-watched titles.

### 6.4 AI & LLM Integration ([llm.py](file:///c:/Users/vansh/Downloads/one_user_one_agent_data_pack%20(4)/one_user_one_agent_pack/submission/src/oua/llm.py))
The LLM is strictly restricted to three bounded jobs:
1. `choose_goal`: Selects among rule-eligible goals using high-level viewer features.
2. `compose_page`: Selects hero and rows within candidate recommendations.
3. `write_copy`: Drafts push/WhatsApp text conforming to tone and length parameters.

**Guardrails**:
- **Prompt Isolation**: Catalogue descriptions and search terms are treated strictly as literal data, preventing prompt injection.
- **Privacy Preservation**: No user PII or raw timestamps are sent to the model.
- **Grounding Verifier**: Any title hallucinated by the model that does not exist in the candidate pool is purged. If all titles in a row are ungrounded, the system reverts to deterministic rule-based composition.
- **Fallback Templates**: If LLM copy fails verification checks (forbidden words, pricing mismatches, length limits), it automatically falls back to pre-approved static templates from [agent_policy.json](file:///c:/Users/vansh/Downloads/one_user_one_agent_data_pack%20(4)/one_user_one_agent_pack/submission/config/agent_policy.json).

### 6.5 Latency Budgets & Fault Tolerance ([dependencies.py](file:///c:/Users/vansh/Downloads/one_user_one_agent_data_pack%20(4)/one_user_one_agent_pack/submission/src/oua/dependencies.py))
- **3-Second Page Target**: Decision latency must finish before the user reaches the app home screen.
- **Load Shedding**: If queue wait exceeds 5 seconds, all optional LLM calls are skipped, falling back instantly to deterministic code paths.
- **Circuit Breakers**:
  - LLM timeout: 1.5 s. One timeout disables further model calls for that decision.
  - Data store timeout: 2.0 s (with 1 retry). If degraded, the agent falls back to a cached snapshot.
  - Call Budgets: Max 20 tool calls and 8 model calls per decision. If exceeded, the system fails closed gracefully.
