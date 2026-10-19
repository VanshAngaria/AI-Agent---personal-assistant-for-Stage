# Known limitations (foundation)

Intentionally incomplete:

- No event/profile/catalogue ingestion
- No source-precedence reconciler
- No planner, verifier, mock LLM, or replay harness
- No homepage/message builders
- `sample_run/` artifacts are not generated
- Confidence is a field on the schema only; no scoring method is implemented
- Title IDs on a `PUBLISHED` decision are not checked against the catalogue yet
- `in_app_message` payload is unspecified in the pack; not modeled beyond `ActionType`
- G06 “recently lapsed” and G07 series eligibility are not interpreted
- SQLite `created_at` is store metadata (`datetime.now(timezone.utc)`), not viewer evidence
- `README.md` in the data pack still contains leftover text from a deleted previous build; this package does not depend on it

What this stage does **not** prove: latency budgets, holdout isolation at decision time, messaging policy, impact vs holdout, or surprise-event handling.
