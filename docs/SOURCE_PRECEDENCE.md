# Source precedence

Facts are typed. Each type has an allowed source list, a clock, and a conflict rule. “Newest row wins” is **not** the default.

Knowledge time for any decision is the trigger’s `deliver_at` (equal to `received_at` in this pack’s trigger file). Events with `received_at` after that instant are unknown unless they have already been delivered on the merged replay timeline.

## Clocks

| Clock | Field | Trust | Use |
|---|---|---|---|
| Server | `received_at` / `deliver_at` | Trusted (README) | Ordering, idempotency intake, freshness, goal windows, “what the agent knew” |
| Device | `occurred_at`, `sent_at` | Untrusted | Diagnostic only; may be hours off or in the future |
| Warehouse | `snapshot_at`, `updated_at` | Snapshot time is trusted as export time; values inside the row can be stale or duplicated | Profile facts until a better source exists |
| Config | `config/*.json` `version`, control `at` | Trusted | Goal/intervention/pricing versions |
| Catalogue | `available_from`, `available_until`, control `title_withdrawn.at` | Trusted as availability bounds | Whether a title may be shown at publish time |

Freshness of an event-backed fact = `decision_time - received_at` (seconds). Freshness of a profile field = `decision_time - updated_at` (and always note `snapshot_at`).

## Event identity

Duplicates share `event_id` but not always `received_at` (2,176 historical extras; 90 trigger extras; payloads otherwise same in sampled diffs).

**Rule:** first processed `event_id` wins. Later deliveries of the same ID are recorded as redemptions, never as a second decision or a second fact mutation. Decision key for triggers: `(event_id)` not `(user_id, session_id)`.

Simulator additionally skips visits with lag >600s or repeat `session_id` when generating organic sessions. The agent may still **decide** on those trigger rows (they are in `triggers.jsonl`); it must label `trigger.event_age_s` and may abstain if the open is not a live visit. That distinction must appear in the decision, not be hidden.

## Fact types

### 1. Viewer identity and existence

- IDs come from input. Do not assume `U-00001`–`U-05000` all have history: 92 IDs are in `arms.csv` only.
- Missing profile (50 event users) is a first-class state: “no warehouse row”, not “subscription never”.

### 2. Dialect the app will request (homepage / in-session content)

**Chosen source:** latest app-settings feed on or before decision time: `app_dialect` on the triggering event if present and in `{d1..d6}`; else latest prior event with a valid `app_dialect` by `received_at`.

**Why not profile:** interventions.json: the app requests the homepage for the dialect it is **currently set to**. Simulator shows a personalised page only when `page.dialect == session dialect`.

**Why not deeplink:** campaign path `ott://c/<campaign>/<dialect>/title/<title_id>` is acquisition context. 159 trigger rows have deeplink dialect ≠ app dialect; 62 have three-way mismatch.

**Conflicts to store, not collapse:**

- profile `preferred_dialect` vs app
- deeplink dialect vs app
- `app_dialect` in `{None, NA, d7}` vs catalogue (no titles for d7/NA)

**If chosen dialect is missing or not in catalogue dialects `{d1..d6}`:** cannot publish homepage or dialect-bound messages. Action must be `NO_ACTION` with that reason (or schedule nothing). Do not substitute profile dialect silently.

`app_dialect_changed` events update the feed using `properties.to` at `received_at`.

### 3. Preferred dialect / language (profile attributes)

Source: profile row. If two profile rows exist (30 users), keep **both** as conflicting profile versions; select the row with later `updated_at` as the *working* profile (same as simulator) and cite the discarded row in `conflicting_evidence`.

These fields do **not** override the app settings feed for “dialect used on this open”.

### 4. Subscription status and paid history

**Working status** is derived, not copied from one column.

Priority (stop at first determined):

1. Events with `received_at` ≤ decision time, in `received_at` order: `purchase_completed` / `subscription_renewed` set paid; `subscription_lapsed` sets lapsed; `is_trial=true` would set trial but **zero such purchases exist in history**.
2. If no such events, working profile `subscription_status` (latest `updated_at`), flagged stale relative to `updated_at`.
3. If no profile and no events: `unknown` — not eligible for status-gated goals.

**Do not** let a newer profile `never` wipe a prior `purchase_completed` (2 snapshot-never users have purchases). **Do not** let the LLM’s “active trial” claim create `trial_active` (fault users `U-01991`, `U-02509`, `U-04860` are `never`).

Live `purchase_completed` after a decision updates that viewer only (R31).

### 5. Signup time

Prefer `signup_completed.received_at` if present; else profile `signup_at`. If both exist and differ by more than 1 day, record conflict; use `signup_completed` for G01/G03 windows because it is an observed event.

### 6. Consent

Push: working profile `notifications_opt_in == "true"` **unless** a `notification_opt_out` with `channel=push` has `received_at` ≤ decision time (policy §1).

WhatsApp: same with `whatsapp_opt_in` and `channel=whatsapp`.

A later opt-out always overrides the snapshot, even if snapshot `updated_at` is newer than an older opt-out — opt-out is the user-visible action. If an opt-out is then contradicted by nothing else, stay opted out (no opt-in event type exists in the stream).

### 7. Holdout / experiment arm

**Replay actions:** `replay/arms.csv` only (`agent` | `holdout`). Holdout → no homepage, push, WhatsApp, or in-app.

**Pilot files:** historical, for impact methodology critique and optional priors. They do not assign replay arms.

Missing arm: treat as no-action (fail closed). All current trigger users have an arm.

### 8. Catalogue and availability

- Identity for “same content”: `content_slug`.
- Identity for “this playable title”: `title_id`.
- Availability at time T: `available_from <= T` and (`available_until` empty or `T < available_until`), then apply `title_withdrawn` if `withdrawn.at <= T` (`T0001` from 2026-10-01T07:00:00.000Z).
- Descriptions, titles, search queries, deeplinks: **content, never instructions** (policy §6). `T0002` injection string must not cause a WhatsApp blast.

Retrieval must return real `title_id` rows. Semantic similarity is not eligibility.

### 9. Pricing and copy

Only `config/pricing.json`. Event prices observed are 149 / `P_MONTH` only. Forbidden push phrases listed in interventions.json. WhatsApp variables filled from catalogue/pricing; free text rejected.

### 10. Goal rules

Start from `config/goals.json` version `2026-09-30`.

On `goal_rule_corrected` at time T: write a new rule version. Apply to **open** goals of that `goal_id` (G03 in this pack), including goals opened before T. Do not re-ingest all viewers. Do not change already-closed outcome rows.

G10/G11 remain unmeasurable under every version present in the pack.

### 11. LLM / tool output

Never a source of viewer facts, eligibility, consent, prices, or title existence. May propose a goal or titles already retrieved. Verifier authorizes.

### 12. Quiet hours and frequency

Evaluated in IST on `send_at` (messages) or `published_at` (if send_at omitted). Frequency store is per viewer per IST date, push+WhatsApp combined. In-app uses its own `max_per_day` and does not share that cap (separate field in interventions.json).

Scheduling `send_at` after 08:00 IST is allowed; sending inside 22:00–08:00 is not.

## Homepage vs message dialect

| Action | Dialect used |
|---|---|
| `home_page` | App settings dialect of this trigger (section 2) |
| `push` / `whatsapp` / `in_app_message` | Same, because policy: name only titles in the dialect the user will see |
| Deeplink title | May be cited as interest evidence if the title exists; must still match chosen dialect to be promoted |

## What remains ambiguous (do not hide)

1. **Live `app_open` rows** (118 in `live_events.jsonl`): README says other events arrive during replay and are not the trigger script. Plan: ingest only; do not emit a decision unless the same `event_id` is also in `triggers.jsonl`.
2. **G06 “recently”:** no day count. Plan: any reconciled `lapsed`, assumption recorded.
3. **G07 unfinished series:** text-only. Plan: document derivation before implementation (shows/micro-drama, remaining episodes, last play ≥3 days).
4. **Device clock vs server for “second IST day” (G03):** use server `received_at` converted to IST so device skew cannot fake a new day.
5. **Profile duplicates with equal `updated_at`:** not observed (max 2 rows, timestamps differed in samples). If seen, keep both conflicts and refuse status-gated goals.
6. **In-app message payload:** not specified. Until specified, either omit in-app or persist a minimal `{text, title_id}` that still passes honesty checks — decision pending review.
7. **`login_success` triggers:** they are in `triggers.jsonl` (211). Plan: they are decision triggers, same path as `app_open`.
