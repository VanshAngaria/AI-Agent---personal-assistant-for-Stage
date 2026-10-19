# AI collaboration log

## This stage (foundation)

Cursor Grok 4.6 authored the `submission/` tree from the Prompt 0 recon docs and the Stage-1 instruction.

Checked by the operator of this session:

- Schema combinations were encoded as Pydantic validators, then tested (not accepted on generation alone).
- Goal catalog is the pack `config/goals.json` copy. G10/G11 are unmeasurable because `success.event` is null / `tv_app_open`; tests reject proxy signals.
- Tests use `TEST-VIEWER-*` and `test_fixture` sources so they do not pretend to be pack evidence.
- `pytest` was run after the files were written; results belong in the chat report, not as invented pass counts.

## Rejected suggestions (not present in code)

- Hardcoding pack user IDs or title IDs as “sample decisions”
- Filling empty evidence lists with placeholder event IDs
- Treating G10 as measurable via a proxy event
- Implementing ingestion or a planner in the same change set
