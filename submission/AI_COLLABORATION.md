# AI collaboration log

## Tools and roles

* **Earlier attempt (Cursor + Grok):** produced a "foundation" (schema, SQLite store, goal selector, catalogue
  retrieval) and the reconnaissance notes in `docs/`. On review it had no decide → act → measure loop, its README
  described a frontend and test counts that did not exist, and its G03 correction crashed on the real control event.
  The code was discarded. The reconnaissance notes were kept as input.
* **This build (Claude Code, Claude Opus 5.5):** I directed the work step by step. I asked for a review of the
  existing code against the brief, then an architecture proposal, which I approved before any code was written.
  Claude then wrote the code and tests, ran the full replay after each significant change, and inspected the
  decisions and reports. I reviewed the outputs listed below.

## How the output was checked

1. **Every behaviour claim is backed by a run.** Each numbered claim in `ARCHITECTURE.md` and
   `sample_run/final_report.md` comes from a generated file. The reports are produced by code (`python -m oua impact`),
   not typed by hand.
2. **The full replay was inspected, not just the tests.** After the first end-to-end replay, the decision mix was
   tabulated: status by arm, no-action reasons, goals, model outcomes, failed checks. This exposed problems unit
   tests had not (items 1 and 2 below).
3. **Mutation testing.** Eight key behaviours were broken on purpose, one at a time, and the suite was run against
   each:
   * stale-trigger detection
   * rule-correction inheritance
   * event deduplication
   * rejecting unsupported model claims
   * quiet hours
   * the holdout gate
   * judge attribution
   * opt-out precedence

   Six were caught at first. Event-redelivery counting and late-upload attribution were **not** caught, so two
   tests were added. All eight are now caught.
4. **The simulator acted as an external check.** It reported 0 policy problems for the 1,153 published actions,
   0 holdout actions and 0 dropped rows.
5. **Determinism.** Two runs on the same seed produced identical numbers.

## What was rejected or corrected

1. **Planner bug caught by the verifier.** The first replay published only 77 pages. The independent verifier had
   rejected 669 pages because the planner put two language variants of the same content (same `content_slug`) in
   one row. The planner was fixed, not the verifier. This is the case the "checks that can disagree with the
   decision step" requirement is meant for.
2. **An over-broad reading of G07.** The first reading ("any started series with episodes left, idle 3+ days")
   committed G07 for about half of all viewers, mostly people who had sampled one micro-drama episode. It was
   narrowed to series the viewer was actually following, and the reason is recorded in every G07 decision.
3. **A wrong test expectation.** A judge test expected "inconclusive" where the correct answer was "failed": the
   observed data extended past the deadline. The test was fixed after reasoning it through; the code was not bent
   to match the test.
4. **Honest-copy decisions.** The `wa_new_episode_v2` template ("Naya episode aa gaya!") was not used because new
   episodes cannot be verified. A draft push line saying "watch it today" was rewritten because it reads like an
   invented deadline.
5. **No hosted model provider.** Shipping an untested hosted-provider client was declined; the mock plus the
   provider protocol is what the brief needs.
6. **No crash-recovery gap.** The first version treated a trigger that was claimed but never decided as done, so
   a crash between claim and commit would have lost it. It now re-decides on redelivery, with a test.
7. **Output written to the wrong place.** An early smoke run wrote outside the project directory. It was moved to
   a scratch area and the CLI default was kept at `submission/sample_run/`.

## Where AI output should still be read with care (applies to every iteration)

* The goal priority order and the readings of G02/G06/G07/G12 are judgement calls. They are documented in
  `ARCHITECTURE.md` and are the main things to challenge.
* The confidence heuristic's weights were chosen, not fitted.

## Later iterations

### Visual console (built, then removed as out of scope)

I asked for a Streamlit console, then removed it: the brief values a working decide, act and measure loop over a
UI, and `python -m oua audit` already shows any viewer's state. Building its live-run page did expose a real agent bug: after a restart, the agent
rebuilt its rules from `goals.json` and forgot the persisted G03 correction and the title withdrawal. It now replays
its control history on start-up, with a test. Every page was smoke-tested headlessly and then rendered in a real
browser and inspected. Layout problems found that way were fixed: a hero number that lost its styling, overlapping
dots, a histogram on an unreadable linear scale, and collapsed chart rows.

### Removing hard-coded behaviour; adding a real model role and retrieval

I pointed out that the agent used no LLM or RAG and that much of its behaviour was fixed in code. I asked for both to
be fixed where they genuinely help.

What was built:
* **Policy file.** Goal readings, thresholds, priorities, per-goal strategies, copy, safety patterns, retrieval
  weights and confidence weights moved into `config/agent_policy.json`, which is validated at load.
* **Generic goal engine.** Checks and matchers driven by `goals.json` plus the policy; no goal ids in code.
* **Retrieval.** BM25 retrieval over each viewer's own searches and plays.
* **Model tasks.** Three bounded model tasks, with a real Claude provider on the official SDK.

How it was checked:
* **Tests caught two latency regressions** as soon as the model was wired in:
  * a 300-open burst breached 60 s for 95 decisions;
  * the 45 s timeout fault stacked across three model calls.

  The fix was real-time behaviour, not looser tests: model calls are deadline-aware, there is no second wait after a
  timeout, and the system sheds load when the queue backs up.
* **Looking at the console found noisy retrieval.** A viewer who searched title words was shown a hit that matched
  only the generic word "old". The score distribution across all 5,502 hits was examined (median 38, about 5% were
  single generic words scoring around 2), and a policy `min_score` of 5 was set from that evidence.
* **A scan for leftover goal ids** found one in the verifier (a G05 special case). It was replaced with a rule about
  the status value "never" itself.
* **Mutation testing of the new behaviours.** Thirteen more were broken on purpose:
  * grounding of IDs and of the hero;
  * rejecting unsupported claims;
  * load shedding, and no second wait after a timeout;
  * refusing uninterpreted goals;
  * the policy stale threshold;
  * retrieval excluding watched titles;
  * the model declining a follow-up;
  * the template fallback for bad copy;
  * the three earlier ones re-checked.

  Three were missed at first: two because a second safeguard masked the first, one because no test existed. The
  tests were tightened to assert *which* safeguard fired. All are now caught.

Suggestions rejected:
* **An embedding/vector database.** The pack's search queries are title words, lexical retrieval matches them well,
  and an embedding model would add a network dependency or a large offline model for evaluators.
* **Letting the model decide eligibility, or pick titles freely.** The brief says a single prompt that picks titles
  scores poorly, and that model text is not evidence. The model only chooses within what code produced.
* **OpenAI-compatible shims for Claude.** The official SDK is used, so behaviour (structured outputs, fallbacks, typed
  errors) is the documented one.
* **Running a live Claude replay automatically.** It costs money and sends pack-derived features to an API.
  The provider is tested against a local fake API instead, and the live run is left to the evaluator's choice.
