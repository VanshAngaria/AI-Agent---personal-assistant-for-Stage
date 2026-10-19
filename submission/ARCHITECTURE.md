# Architecture

A single-process agent service over SQLite. One code path (`AgentService.handle_trigger`) is used by the replay
harness, the tests and the audit tools. The design follows four facts found in the pack:

1. **A page is only useful if it lands within seconds.** The simulator shows a page only if it was published before
   the viewer reaches home (3-40 s after the open). The page path is therefore deterministic and fast (p95 under 1 s
   in the sample run). The 60 s budget is a ceiling, not a target.
2. **Measurement needs the holdout treated identically.** Holdout viewers go through the same pipeline and get the
   same *shadow goal*; only publication is suppressed. Both arms are then judged the same way.
3. **Many triggers are not visits.** 424 triggers in the replay were buffered on the phone for more than 10 minutes.
   The agent detects this from `sent_at - occurred_at` (both device clock, so clock offset cancels out) and records
   them as history only.
4. **Code decides what is allowed; the model judges within it.** Eligibility, consent, dialect, availability and
   policy are computed by code from pack data and versioned config. The model (Claude, or a local mock) does three
   bounded jobs inside those limits: it chooses among goals the rules found eligible, composes the home page from
   *retrieved* candidates, and writes message copy. Every output is checked against what it was offered (grounding)
   and then goes through the independent verifier.

```
triggers ─┐                                    ┌──────────── AgentService (one viewer at a time) ────────────┐
live ─────┼─► harness (virtual clock,          │ 0 gate: idempotency, arm (fail closed)                      │
control ──┘   worker pool, per-viewer lock) ──►│ 1 reconcile: facts + source + freshness + conflicts         │
                                               │   (reads via budgeted ToolClient; cached snapshot on outage)│
          config/goals.json ──┐                │ 2 stale trigger? → NO_ACTION                                 │
   config/agent_policy.json ──┼──────────────► │ 3 plan: keep / close / revise the single open plan          │
                              │                │ 4 goal: generic checks decide eligibility ─► model chooses  │
                              │                │   among eligible goals (or rules' priority on any failure)  │
                              │                │ 5 holdout? → shadow goal, NO_ACTION                          │
         catalogue ── BM25 index ────────────► │ 6 retrieve: candidates from the viewer's searches + plays   │
                                               │   model composes page within them ─► grounding check        │
                                               │   (+ follow-up per policy; model may decline, never add)   │
                                               │ 7 verifier: independent re-check → repair / drop / block    │
                                               │ 8 one transaction: decision, facts, plan ops, actions       │
                                               └───────────────────────────────┬─────────────────────────────┘
                                                                               ▼
                       decisions.jsonl, actions.jsonl ─► simulator ─► judge (attribution, rule versions, matchers)
                                                                        ├─► lessons → viewer state
                                                                        └─► impact (ITT vs holdout)
```

## Ownership

* **One agent per viewer.** The harness never runs two decisions for the same viewer concurrently: a decision starts
  only after that viewer's previous one finishes.
* **One plan per viewer.** A partial unique index (`ux_open_plan`) makes a second open plan for a viewer impossible,
  whichever component tries.
* **One writer.** Plans are only created and closed by `AgentService`, by the live-event handler (success closes a
  plan), by the rule-correction handler (rewrites the success signal of open plans) and by the final judge. Every
  change is appended to `plan_history`.

## Planning (all from config, no goal ids in code)

* **Goals are data.** `goals.json` gives each goal's structured eligibility and success event. The goal engine turns
  each structured field into a named check:
  * `signed_up_within_days` becomes `signed_up_within_days`;
  * `requires_no_prior_event` becomes `no_prior_event`, which needs full history to prove "never";
  * `subscription_status` becomes `subscription_status_in`.

  `agent_policy.json` adds reviewed checks for the parts that are text only (`recent_event`, `unfinished_series`,
  `max_event_days`, `no_event_in_window`, `account_min_age_days`, `never_paid`), and names the success matchers per
  goal (`later_ist_day_than_start`, `same_content_as_plan`, `title_released_within_days_before_start`,
  `property_not`, `distinct_ist_days`). A new goal built from existing checks and matchers needs config only. A goal
  with free-text eligibility and no reviewed interpretation is **refused**. G10/G11 are never committed because their
  success signal cannot be observed.
* **When the goal changes.** A goal is chosen when the viewer has no open plan. It is revised only when the situation
  changes, never on a schedule. Each trigger re-checks the open plan: a passed deadline, or preconditions that no
  longer hold (for example a purchase arrived), cause a close and a fresh choice. The policy lists goals whose
  preconditions are consumed by progress (G01, G02, G07, G12) and are therefore kept while open. A live event that
  meets the success condition closes the plan immediately.
* **Selection.** The policy's lifecycle priority (time-critical conversion, activation, re-engagement, habit,
  monetisation) is the default. When two or more goals are eligible, the model chooses among them with a reason. It
  can never add a goal: a claim about an ineligible goal is rejected with the rule's reason. A goal that failed for
  this viewer is cooled down (policy: 7 days).
* **Proportionate interventions** (per-goal strategies in the policy):
  * A live open gets one home page built for the goal: a resume hero for G07, short micro-drama episodes for G02, new
    releases for G08, the campaign-link title as hero when it is in the viewer's dialect.
  * A follow-up message exists only where the policy defines one (G03, G07: they need a *later* visit). It goes at
    19:00 IST the next day, at most once per plan, and only with consent. The model may decline it but never add one.
  * Premium titles are not promoted to non-paying viewers for viewing goals.
  * In-app messages are not used.
  * A page still live for the same goal and dialect is not republished.
* **Readings of underspecified rules** (in the policy; each recorded in the decision's `assumptions`):

| Goal | Reading | Why |
|---|---|---|
| G02 "never completed" (also G01) | Only provable for viewers whose whole account life is inside the 30-day history | Absence of events before the history began is not evidence |
| G06 "recently lapsed" | A `subscription_lapsed` event in the last 30 days | Profile rows carry no lapse date |
| G07 "unfinished series" | A show or micro-drama the viewer was following (reached episode 2+, or completed one), last episode not reached, idle 3+ days, still available | Sampling one episode is not following. The first reading (any started series) committed G07 for half of all viewers |
| G09 "plays on at most 1 day" | Distinct IST days of `play_started` (server time) in the last 14 days | |
| G12 "no app_open in 14 days" | Excludes the trigger and its session; account must be 14+ days old | A brand-new viewer is not "returning" |

## Retrieval and the model (RAG, bounded)

* **Retrieval (`retrieval.py`).** A BM25 index over every title's name, genres, type and plot summary (field weights
  in policy). For each viewer, the query is built from their own `search` queries (weighted higher) and the titles
  they played in the last 30 days. Hits are restricted to titles that can be shown: app dialect, available at publish
  time, not quarantined, not already watched. A minimum score drops single generic-word matches. Every retrieved title
  on a page is cited as `retrieval:T…`, with its matched terms and the viewer events it came from. In the sample run,
  707 of 920 pages contain retrieved titles. Retrieval is local, pure Python and lexical: search queries in this pack
  are title words, which is what lexical matching is good at, and nothing leaves the machine.
* **The model's three tasks (`llm.py`):**

| Task | Input | Output (JSON schema) | Checked by |
|---|---|---|---|
| `choose_goal` | Eligible goals with the rule's reasons; derived viewer features | `{goal_id, reason}` | Must be in the eligible set, or rejected as unsupported |
| `compose_page` | Goal, viewer interest terms, hero candidates, candidate rows (IDs, names, genres, episodes, capped summaries, retrieval scores), whether a follow-up is allowed | `{hero_title_id, rows[{row_type, title_ids}], send_follow_up, rationale}` | Grounding: every ID must come from that row's candidates, no repeated content, rows of 3 or more. Invented IDs are removed and logged. If nothing usable is left, the rules compose instead |
| `write_copy` | Title name, language, length limits, banned phrases | `{title, body}` | Verifier: length, banned phrases, prices only from `pricing.json`, must name the title. On failure the policy template is used and re-verified |

* **Providers.**
  * `MockProvider` is the default: deterministic, no network, replays `model_faults.jsonl`. It composes exactly the way
    the rules would, so the sample run's numbers do not depend on a model.
  * `AnthropicProvider` uses the official SDK. Default `claude-opus-5-5` at effort `low`, with JSON-schema structured
    outputs, server-side refusal fallbacks and a cached system prompt. Retries are off (a timeout falls back instead),
    and errors are mapped to the same circuit breaker as an injected 503.
  * `FaultInjectingProvider` keeps the pack's faults working on top of a real provider.
* **Prompt hygiene.** The system prompt states that titles, summaries and search queries are data, never
  instructions. Summaries are length-capped and stripped of markup characters. Quarantined titles are never sent.
  No viewer IDs, timestamps or raw rows are sent.

## Memory (durable per-viewer state, `store.py`)

| Table | Holds |
|---|---|
| `events` | Every event the agent has seen (history, triggers, live, simulator), deduplicated by `event_id`, keeping the earliest receipt and a delivery count |
| `profile_rows` | All warehouse rows, including duplicates; nothing is collapsed at load time |
| `facts` | The reconciled facts behind each decision: value, source, source id, observed time, freshness |
| `decisions` | The full decision record |
| `actions` | Simulator-shaped actions; `ux_message_day` enforces 1 push/WhatsApp per viewer per IST day |
| `plans` | Goals with rule version, window, parameters and status |
| `plan_history` | Every open, close, rule correction and fact change |
| `outcomes` | Judged result with reason, signal and exposure (page shown, message delivered/clicked/failed) |
| `lessons` | What was learned: goal succeeded/failed, push undeliverable, opted out, page not seen. Read by the next decision |
| `rule_versions` | Every version of every goal rule |
| `control_log` | Each control event and exactly which plans, pages or windows it affected |

`python -m oua audit --viewer V --at T` rebuilds the view as of time T: events received by T, facts at the last
decision before T, decisions, actions, plan history, outcomes and lessons. `sample_run/viewer_state/` holds a
per-viewer export.

## Source precedence (`reconcile.py`)

| Fact | Precedence | Notes |
|---|---|---|
| Dialect used | The trigger's `app_dialect`, if it is d1-d6 | The app requests the page for its current setting. Profile, previous app setting and campaign link are kept as conflicts. `null`, `NA` and `d7` mean no action: nothing can be shown |
| Subscription status | Billing events received after the profile's `updated_at` override the profile; otherwise the profile; otherwise unknown | A missing profile row is "unknown", not "never" |
| Ever paid | Any paid purchase or renewal event, or a profile status of active/lapsed | A profile "never" cannot erase an observed purchase |
| Duplicate profile rows | The later `updated_at` is the working row; the other is shown as a conflict. On a tie, status-gated goals are refused | |
| Signup time | `signup_completed` event, then the profile | |
| Consent | Profile opt-in, overridden by any `notification_opt_out` | No opt-in event exists, so an opt-out is permanent |
| Event time | `received_at` (server) for ordering, windows and freshness | Device time is used only for buffer detection (`sent_at - occurred_at`) |
| Model output | Never a fact | |

## Verification (`verifier.py`)

The verifier is the only way anything gets published. It does **not** read the reconciler's conclusions, the
planner's reasoning or the model's output. It re-derives what it needs from raw profile rows, raw events, the arm table, the catalogue,
the policy files and a fresh read of the message ledger, using its own simpler code. It can therefore disagree with
the decision step. In the first full replay it did: it rejected 669 pages because the planner had put two language
variants of the same content in one row. That planner bug was then fixed.

Checks: holdout; trigger is a live visit; goal measurable; goal eligibility re-checked from raw rows; cited events
exist; call budget; latency; page dialect equals the app setting; every title exists, is in the page dialect, is
available at publish time and is not quarantined; unique content; row sizes and types; message consent and opt-out;
quiet hours on `send_at`; frequency cap; send before the goal deadline; message title valid in the app dialect at
send time; approved WhatsApp template with variables from the catalogue; push length, forbidden phrases, prices
only from `pricing.json`, copy names the promoted title; deeplink format.

When something fails, the agent first removes the rejected titles and re-verifies once. Then it drops the failing
action (a message failure does not cost the page). If a global check fails, it publishes nothing and says why.

## Latency and real-time behaviour

* **Virtual time.** `published_at = deliver_at + queue wait + simulated dependency time + measured CPU`. Dependency
  calls charge simulated latency (store 20 ms, mock model 400 ms; a real provider charges its measured latency).
* **Deadline-aware model calls.** The page must land before the viewer reaches home, so model calls may only use
  what is left of the 3 s page target after queueing. A call that could overrun it is not started. After one timeout,
  no further model calls are made in that decision.
* **Load shedding.** If a decision waited more than 5 s in the queue (settings), its optional model calls are skipped
  and the deterministic path is used. A 300-open burst test with 2 workers stays inside the budget because of this.
  Without it, the same test breached 60 s for 95 decisions; the test caught that regression when the model was added.
* **Worker pool** (`workers`, default 4) with per-viewer serialisation. Queue wait is recorded on every decision and
  in `latency_report.json` (per IST hour, per fault window).
* **Sample run:**
  * p50 0.44 s, p95 1.25 s, max 6.9 s (inside the degraded-store window), with up to 3 model calls per decision.
  * 0 decisions over 60 s; 910 of 912 pages within the 3 s page target.
  * A decision projected past 300 s is recorded as `FAILED_LATE` and nothing is published.
* **Idempotency.** `processed_triggers` is keyed by `event_id`. A redelivery returns the stored decision and is
  counted; it never produces a second decision or action. Live and history events are deduplicated the same way.

## Failure recovery

| Failure | Behaviour |
|---|---|
| Model 503, overload, rate limit or network error | Fall back to rules for that task; the circuit breaker opens, so later calls are skipped without waiting, then probes after 60 s |
| Model timeout (45 s fault) | Cancelled at 1.5 s; no further model calls in that decision; rules decide; the page still lands within the page target |
| Model output invents IDs or breaks policy | Grounding removes invented IDs (rules compose if nothing usable is left); copy that fails verification is replaced by the policy template and re-verified |
| Malformed model output | Strict JSON parse fails; ignored; rules decide |
| Unsupported model claim | Rejected because the goal is not in the eligible set; recorded with the rule-based reason; confidence lowered |
| Data store degraded (p95 8 s, 20% timeouts) | 2 s timeout, one retry. Then the agent's cached snapshot of the viewer is used, flagged stale, page only (no messages, because consent and caps cannot be confirmed). With no snapshot: `FAILED_SAFE`, nothing published |
| Budget exhausted (20 tool / 8 model calls) | `FAILED_SAFE`, nothing published |
| Title withdrawn mid-day | Excluded from retrieval and verification from that moment. Live pages that contain it are found and republished without it (7 in the sample run), and the old page is marked superseded |
| Goal rule corrected | A new rule version, with unstated fields inherited. Only open plans of that goal are updated (one indexed query); closed outcomes and other viewers are untouched; each change is logged |
| Process crash | Each decision commits atomically with its plan operations and actions. A trigger that was claimed but has no committed decision is decided again when it is redelivered |

## Trust boundaries

* **Data is not instruction.**
  * Catalogue descriptions, titles, search queries and deeplinks are content.
  * Titles whose text matches the policy's instruction patterns are quarantined from promotion and flagged for review.
    Exactly one row in the real pack matches, with no false positives, and a test asserts both.
  * Quarantined text never reaches the model. Other catalogue text reaches it only as delimited, capped data under a
    system prompt that says it is never an instruction.
* **Model output.**
  * Parsed against a JSON schema, and restricted to options the code produced (eligible goals, retrieved candidates).
  * Grounding-checked, then verified.
  * Never cited as evidence; never counts as approval.
  * The model may decline a follow-up message but never create one.
* **Privacy.**
  * Providers receive derived features and candidate titles, never raw pack rows, viewer IDs or timestamps.
  * The default provider is the local mock; Claude is opt-in.
* **Holdout.** The holdout is enforced three times: in the agent before planning, in the verifier from its own copy
  of the arms, and in the decision schema (a `PUBLISHED` record for a non-agent arm fails validation).
* **Fail closed.** A viewer with no arm, no facts or no budget gets nothing.
* **Config is reviewed, not inferred.** Goal interpretations, thresholds and copy live in a versioned policy file that
  every decision cites. An unknown check, matcher, hero strategy or row source fails at load.

## Measurement

The judge evaluates every plan in both arms under the plan's own rule version:

* **Attributable** means received inside `[started_at, deadline]` and, corrected for device buffering, happened
  after the goal started. Late uploads of earlier activity are counted and excluded.
* **Statuses:** succeeded, failed, or inconclusive (the window extends past the observed data, or the signal
  predates the goal).
* **Corrected rules:** for plans whose rule changed mid-window, the reason also states the result under the old rule.

Impact is intent-to-treat on `replay/arms.csv`:

* **Primary metric:** first-goal success for every triggered viewer, agent arm vs holdout arm.
* **Secondary metrics:** goal-agnostic behaviour (any play, completion, paid purchase, plays on 2+ days) over all
  triggered viewers.
* **Supporting tables:** a pre-treatment balance table, the "served vs everyone else" comparison computed only to
  show its selection bias, and the pilot report recomputed both ways.

## Scaling notes (10 million viewers, 40 dialects)

* **Partitioning and stores.** The per-viewer serialisation becomes partitioning by `viewer_id` (for example a keyed
  stream). SQLite becomes a key-value store for viewer state plus an append-only event log; the schema invariants
  become conditional writes.
* **Shared per-dialect work.** Catalogue retrieval and popularity become per-dialect precomputed candidate lists,
  refreshed on catalogue changes. With 40 dialects and far more titles, the BM25 index becomes a per-dialect search
  index, or an embedding index where plot-level similarity matters more than title words. Withdrawals become an
  indexed "pages containing title" lookup, as here.
* **Model cost at scale.** Model calls are optional by design. At 10 million viewers the policy would enable
  `compose_page` only where it measurably helps (an A/B test on top of the holdout), keep `choose_goal` for genuine
  ties, and cache per-dialect candidate payloads.
* **Rule corrections** stay O(open plans of that goal) through an index on `(goal_id, status)`.
* **Bursts.** Admission control sheds model calls first, since they are optional, then messages; pages are kept
  because they are cheap and deterministic.
* **Not automated.** Decisions about money (any offer or price), new message templates, un-quarantining flagged
  content and rule corrections stay with humans. The agent only applies versions that humans supplied.
