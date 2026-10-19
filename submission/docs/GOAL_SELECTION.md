# Goal selection (deterministic)

Source rules: pack `config/goals.json` version `2026-09-30`, plus documented interpretations below. Viewer-specific IDs are never used as rules.

## Measurability

| Goal | Success event | Selectable? |
|---|---|---|
| G01–G09, G12 | Named event present in `data/events.jsonl` / simulator | Yes, if eligible |
| G10 | `success.event` is JSON `null` | Never. No proxy. |
| G11 | `tv_app_open` (0 rows in the pack) | Never. No proxy. |

G03 default success is `app_open` on a later IST day. Replay control `goal_rule_corrected` at 2026-10-01T08:30:00.000Z changes **open** G03 plans to `play_started` on a later IST day. Closed outcomes are not rewritten. Other viewers are not re-planned.

## Interpretations not present as structured JSON

These are the only source-rule interpretations. They are tested.

**G06 “recently lapsed”.** The JSON only has `subscription_status: ["lapsed"]`. No `within_days` field exists. Eligibility is reconciled status `lapsed`. The eligibility result records assumption `G06 has no recency window in the catalog; lapsed status is used as written.`

**G07 unfinished series.** Eligibility is text-only. A series qualifies when: `content_type` is `show` or `micro_drama`; episode count > 1; completed distinct episodes < count; last `play_started` for that slug is ≥ 3 days before decision time. Films (one episode in the pack) cannot qualify.

**G09 play-day cap.** Text says “plays on at most 1 day in the last 14 days.” Count distinct IST dates of `play_started.received_at` in the 14 days before `decision_time`. Zero play days counts as at most one.

**G12 absence window.** “No app_open in the 14 days before the trigger.” The current trigger is excluded even if it is `app_open`.

**Signup clock.** G01/G03 windows use `signup_at` from `signup_completed.received_at` if the snapshot has it, else profile `signup_at`. Missing signup ⇒ not eligible (insufficient history), not treated as a new user.

**Paid vs profile `never`.** `has_paid` from purchase/renewal events blocks G05 even if status text is `never`.

## Priority and ties

At most one goal. If an active plan is still eligible under the current rule version, **keep it** (stability). Otherwise pick the first eligible goal in this order:

1. G01 first_play_24h  
2. G03 second_day_return_2d  
3. G02 first_full_episode_3d  
4. G12 return_after_absence_7d  
5. G07 resume_series_3d  
6. G04 trial_to_paid_7d  
7. G05 first_paid_7d  
8. G06 reactivate_lapsed_14d  
9. G08 try_new_release_7d  
10. G09 weekly_habit_14d  

Rationale: earlier lifecycle / tighter windows first, then high-value conversion, then engagement for already-active subscribers. G10 and G11 are omitted.

If nothing is eligible, abstain with the per-goal reasons. Do not invent a goal.

## Deadline

`deadline = decision_time + window_hours` from the **effective** rule (including G03 correction). Goal start is the commit/`decision_time`, as `goals.json` notes.
