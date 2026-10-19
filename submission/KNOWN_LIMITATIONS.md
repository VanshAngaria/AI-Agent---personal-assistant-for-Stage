# Known limitations

These are deliberate scope cuts or honest gaps, roughly in order of how much they matter.

## Model and retrieval

* **The submitted sample run uses the mock model, not Claude.** The mock composes exactly the way the rules would, so
  the sample run's numbers do not depend on a model, and evaluators need no key. Everything around it runs for real on
  every decision: prompts, JSON parsing, grounding checks, verification, the pack's faults, outages, timeouts and load
  shedding. What has **not** been shown is how real Claude choices would change outcomes. Doing that needs a key, a
  budget, and permission to send pack-derived features to the API.
* **The Claude provider is tested against a local fake API, not the live service.** The tests drive the real Anthropic
  SDK and check the request shape, structured output, server fallbacks, error mapping, timeout and refusal handling.
  They cannot show live latency or output quality. With `claude-opus-5-5` at effort `low`, a page composition may
  take longer than the 1.5 s per-call timeout, in which case the rules compose instead (recorded in the decision). A
  faster model can be set with `--llm-model`, or the timeout raised in `config/agent.json`, trading page latency.
* **Retrieval is lexical (BM25), not embeddings.** Search queries in this pack are title words, which lexical matching
  handles well. Plot-level similarity ("a farmer takes on a middleman") is matched only by shared words. An embedding
  index would need an embedding model the evaluators can run offline.
* **Sending candidate titles to a hosted model.** Payloads contain title names, genres and capped plot summaries for
  candidates, plus the viewer's search terms, but no IDs or raw rows. That is still pack-derived content, which is why
  the hosted provider is off by default.
* **Model faults are reached less often than they could be.** The goal choice is offered to the model only when two
  or more goals are eligible, so faults that land on holdout or buffered triggers never reach a model. In the sample
  run, two of the three unsupported-claim faults and the timeout fault fall on such triggers. All fault types are
  covered by tests.

## Measurement

* **No detectable effect in the sample run.** First-goal success is 13.3% (agent) vs 12.3% (holdout), +1.0 points,
  95% CI −2.4 to +4.4. The goal-agnostic metrics are also flat. This is reported as it is. In the simulator, page
  *content* does not affect which title a viewer plays (it picks from the whole dialect catalogue); a page only raises
  the probability of playing. So better choices, whether from retrieval or from a model, cannot show up in these
  numbers.
* **Confidence is not calibrated, and in this run it does not predict success.** It scores evidence quality, with
  weights set in policy. The impact report includes the band table needed to calibrate it over time.
* **Only the first goal per viewer is the primary unit.** Later goals depend on what happened after treatment.
* **One replay day, one seed.** Per-goal cells are small and descriptive only.

## Agent behaviour

* **Goal interpretations are reviewed judgement calls.** G02, G06, G07, G09 and G12 are readings of vague rules. They
  live in `config/agent_policy.json` and are cited in every decision, so they are easy to change, but they are still
  choices.
* **The success matchers are a fixed vocabulary.** A new goal whose success condition needs a new kind of match (not
  one of the five matchers) needs a small code change, which the policy loader enforces by refusing unknown names.
* **The injection screen is pattern-based** (patterns in policy). Differently worded injections could get through the
  screen. They would still only ever be content: they cannot create an action, and the verifier still applies.
* **In-app messages are never used.** The pack defines no payload for them and the simulator does not render them.
* **WhatsApp `dialect_name` is the catalogue's dialect code** (for example `d3`), because the pack has no display names.
* **`wa_new_episode_v2` and `wa_trial_ending_v1` are unused**, because their claims ("new episode", "trial ending")
  cannot be verified from the pack (listed with reasons in the policy).
* **Follow-up messages are scheduled, not conditional**, so a follow-up is sent even if the viewer reached the goal first.

## Real-time and state

* **Knowledge time is approximate.** In replay, a queued decision sees everything delivered up to its own `deliver_at`.
* **The snapshot cache is in memory only.** After a restart, a data-store outage means `FAILED_SAFE` for every viewer.
* **A page cannot be unpublished.** If a withdrawn title leaves a live page with no valid row, the repair is recorded
  as `repair_failed` and flagged.
* **Single process with SQLite.** Concurrency is modelled in virtual time, not executed in parallel.
* **`decisions.jsonl` is large (~30 MB)** because every decision carries its full evidence, goal evaluations,
  retrieval hits, model tasks and a per-call log. That is intentional, for auditability.

## Repository

* `docs/` at the repository root holds the reconnaissance notes written before this build. They predate the
  implementation; `ARCHITECTURE.md` and this file describe what was built.
