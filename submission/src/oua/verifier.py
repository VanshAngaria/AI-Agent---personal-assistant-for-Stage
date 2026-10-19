"""Independent verification gate: the only path to publication.

The verifier deliberately does NOT use the reconciler's ViewerContext or the planner's reasoning. It re-derives
what it needs from raw rows (profile rows, raw events, arms, catalogue, policy files, the action ledger) with its
own, simpler code, so a bug or a persuasive model output upstream can be caught here. Model text is never an input.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from oua.catalogue import CATALOGUE_DIALECTS, Catalogue
from oua.goals import OBSERVABLE_EVENTS, RuleVersion
from oua.planner import MessageProposal, PageProposal
from oua.timeutil import in_quiet_hours, ist_date, parse_ts

PRICE = re.compile(r"(?:₹|rs\.?|inr)\s*(\d+)|(\d+)\s*(?:₹|rs\b|inr\b|rupees?)", re.IGNORECASE)
DEEPLINK = re.compile(r"^ott://title/(T\d{4})$")


@dataclass
class Verdict:
    passed: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    blocking: bool = False           # a failure that forbids any action for this trigger
    page_ok: bool = True
    message_ok: bool = True
    bad_titles: dict[str, str] = field(default_factory=dict)

    def check(self, name: str, ok: bool, detail: str = "", *, scope: str = "global") -> bool:
        if ok:
            self.passed.append(name)
        else:
            self.failed.append(f"{name}: {detail}" if detail else name)
            if scope == "global":
                self.blocking = True
            elif scope == "page":
                self.page_ok = False
            elif scope == "message":
                self.message_ok = False
        return ok


class Verifier:
    def __init__(self, catalogue: Catalogue, interventions: dict[str, Any], pricing: dict[str, Any],
                 arms: dict[str, str], settings, policy) -> None:
        self.cat = catalogue
        self.iv = interventions
        self.prices = {int(p["price_inr"]) for p in pricing["plans"]}
        self.arms = arms
        self.s = settings
        self.stale_after_s = float(policy.trigger["stale_after_s"])
        self.templates = {t["template_id"]: t for t in interventions["whatsapp"]["approved_templates"]}
        self.forbidden = [p.lower() for p in interventions["push"]["copy"]["forbidden_phrases"]]

    def verify(self, *, viewer_id: str, trigger: dict[str, Any], profile_rows: list[dict[str, Any]],
               events: list[dict[str, Any]], goal_id: str, rule: RuleVersion, evidence: list[str],
               page: PageProposal | None, message: MessageProposal | None, publish_at: datetime,
               deadline: datetime, message_days: set[str], tool_calls: int, model_calls: int,
               latency_s: float) -> Verdict:
        v = Verdict()
        known_ids = {e["event_id"] for e in events}

        # -- global checks -------------------------------------------------------------------------------------
        v.check("holdout", self.arms.get(viewer_id) == "agent",
                f"arm is {self.arms.get(viewer_id)!r}; only the agent arm may receive actions")
        sent, occurred = parse_ts(trigger.get("sent_at")), parse_ts(trigger.get("occurred_at"))
        buffered = (sent - occurred).total_seconds() if sent and occurred else 0.0
        v.check("trigger_is_live_visit", buffered <= self.stale_after_s,
                f"trigger was buffered {buffered:.0f}s on the device")
        v.check("goal_measurable", rule.measurable and rule.success.get("event") in OBSERVABLE_EVENTS,
                f"success event {rule.success.get('event')!r} is not observable")
        ok, why = self._eligibility(goal_id, rule, profile_rows, events, publish_at)
        v.check("goal_eligibility", ok, why)
        unresolved = [e for e in evidence if e.startswith("event:") and e[6:] not in known_ids]
        v.check("evidence_resolves", not unresolved, f"cited events not in the data store: {unresolved[:3]}")
        v.check("budget", tool_calls <= self.s.max_tool_calls and model_calls <= self.s.max_model_calls,
                f"tool {tool_calls}/{self.s.max_tool_calls}, model {model_calls}/{self.s.max_model_calls}")
        v.check("latency", latency_s <= self.s.decision_deadline_s,
                f"projected publish {latency_s:.1f}s after trigger exceeds {self.s.decision_deadline_s}s")

        if page is not None:
            self._page(v, trigger, page, publish_at)
        if message is not None:
            self._message(v, trigger, profile_rows, events, message, deadline, message_days)
        return v

    # -- eligibility re-check from raw rows -------------------------------------------------------------------

    def _eligibility(self, goal_id: str, rule: RuleVersion, rows: list[dict[str, Any]], events: list[dict[str, Any]],
                     at: datetime) -> tuple[bool, str]:
        el = rule.eligibility
        prof = max(rows, key=lambda r: r.get("updated_at") or "") if rows else None
        status = prof.get("subscription_status") if prof else None
        upd = prof.get("updated_at") if prof else ""
        for e in events:  # billing events received after the export win
            if e["received_at"] > (upd or "") and e["event"] in ("purchase_completed", "subscription_renewed", "subscription_lapsed"):
                if e["event"] == "subscription_lapsed":
                    status = "lapsed"
                elif (e.get("properties") or {}).get("is_trial") is True:
                    status = "trial_active"
                else:
                    status = "active"
        if "subscription_status" in el and status not in el["subscription_status"]:
            return False, f"raw status {status!r} not in {el['subscription_status']}"
        # A goal gated on status "never" means never paid, whatever the goal id: an observed paid event contradicts it.
        if el.get("subscription_status") == ["never"] and any(e["event"] in ("purchase_completed", "subscription_renewed")
                                    and (e.get("properties") or {}).get("is_trial") is not True for e in events):
            return False, "a paid purchase exists in raw events"
        if "signed_up_within_days" in el:
            su = next((e["received_at"] for e in events if e["event"] == "signup_completed"), None) or (prof or {}).get("signup_at")
            if not su:
                return False, "no signup evidence"
            if at - parse_ts(su) > timedelta(days=el["signed_up_within_days"], hours=1):
                return False, f"signup {su} is outside {el['signed_up_within_days']} days"
        if el.get("requires_no_prior_event") and any(e["event"] == el["requires_no_prior_event"] for e in events):
            return False, f"a prior {el['requires_no_prior_event']} exists"
        return True, ""

    # -- page --------------------------------------------------------------------------------------------------

    def _page(self, v: Verdict, trigger: dict[str, Any], page: PageProposal, at: datetime) -> None:
        app = trigger.get("app_dialect")
        v.check("page.dialect_is_app_setting", page.dialect == app and app in CATALOGUE_DIALECTS,
                f"page dialect {page.dialect!r} vs app setting {app!r}", scope="page")
        rp = self.iv["home_page"]["rows"]
        v.check("page.row_count", len(page.rows) <= rp["max_rows"], f"{len(page.rows)} rows", scope="page")
        v.check("page.row_types", all(r["row_type"] in rp["row_types"] for r in page.rows), "unknown row type", scope="page")
        slugs: list[str] = []
        for tid in page.title_ids():
            t = self.cat.get(tid)
            if t is None:
                v.bad_titles[tid] = "not in catalogue"
            elif t.dialect != page.dialect:
                v.bad_titles[tid] = f"dialect {t.dialect} != page {page.dialect}"
            elif not self.cat.available(t, at):
                v.bad_titles[tid] = "not available at publish time"
            elif t.instruction_like:
                v.bad_titles[tid] = "description contains instruction-like text (quarantined for review)"
            else:
                slugs.append(t.content_slug)
        v.check("page.titles_valid", not v.bad_titles, "; ".join(f"{k}: {x}" for k, x in list(v.bad_titles.items())[:3]), scope="page")
        v.check("page.unique_content", len(slugs) == len(set(slugs)), "a content_slug appears twice", scope="page")
        sizes = [len(r["title_ids"]) for r in page.rows]
        v.check("page.row_sizes", all(rp["min_titles_per_row"] <= n <= rp["max_titles_per_row"] for n in sizes),
                f"row sizes {sizes}", scope="page")

    # -- message -----------------------------------------------------------------------------------------------

    def _message(self, v: Verdict, trigger: dict[str, Any], rows: list[dict[str, Any]], events: list[dict[str, Any]],
                 m: MessageProposal, deadline: datetime, message_days: set[str]) -> None:
        prof = max(rows, key=lambda r: r.get("updated_at") or "") if rows else {}
        consent_field = self.iv[m.channel]["consent_field"]
        opted_out = any(e["event"] == "notification_opt_out" and (e.get("properties") or {}).get("channel") == m.channel for e in events)
        v.check("message.consent", prof.get(consent_field) == "true" and not opted_out,
                f"{consent_field}={prof.get(consent_field)!r}, opt_out={opted_out}", scope="message")
        qh = self.iv[m.channel]["quiet_hours_ist"]
        v.check("message.quiet_hours", not in_quiet_hours(m.send_at, qh[0], qh[1]), "send_at is inside 22:00-08:00 IST", scope="message")
        v.check("message.frequency_cap", str(ist_date(m.send_at)) not in message_days,
                f"a message already exists for {ist_date(m.send_at)}", scope="message")
        v.check("message.before_goal_deadline", m.send_at < deadline, "send_at is after the goal deadline", scope="message")
        t = self.cat.get(m.title_id)
        app = trigger.get("app_dialect")
        title_ok = t is not None and t.dialect == app and self.cat.available(t, m.send_at) and not t.instruction_like
        v.check("message.title_valid", title_ok, f"{m.title_id} not valid in {app} at send time", scope="message")
        if m.channel == "whatsapp":
            tpl = self.templates.get(m.template_id or "")
            v.check("message.template_approved", tpl is not None, f"template {m.template_id!r} not approved", scope="message")
            if tpl and t:
                allowed = {"title_name": t.title, "dialect_name": t.dialect, "episode_number": None, "plan_price": None}
                vars_ok = set(m.variables or {}) == set(tpl["variables"]) and all(
                    (allowed.get(k) is not None and val == allowed[k]) or
                    (k == "plan_price" and self._price_ok(val)) for k, val in (m.variables or {}).items())
                v.check("message.template_variables", vars_ok, "variables not filled from catalogue/pricing", scope="message")
        else:
            copy = m.text or {}
            cp = self.iv["push"]["copy"]
            text = f"{copy.get('title', '')} {copy.get('body', '')}"
            v.check("message.copy_length", 0 < len(copy.get("title", "")) <= cp["title_max_chars"]
                    and 0 < len(copy.get("body", "")) <= cp["body_max_chars"], "title/body length", scope="message")
            bad = [p for p in self.forbidden if p in text.lower()]
            v.check("message.forbidden_phrases", not bad, f"contains {bad}", scope="message")
            v.check("message.prices_exist", self._price_ok(text), "mentions a price not in pricing.json", scope="message")
            v.check("message.names_the_title", t is not None and t.title in text, "copy does not name the promoted title", scope="message")
            v.check("message.deeplink", bool(m.deeplink and DEEPLINK.match(m.deeplink) and m.deeplink.endswith(m.title_id)),
                    f"deeplink {m.deeplink!r}", scope="message")

    def _price_ok(self, text: str | None) -> bool:
        for a, b in PRICE.findall(text or ""):
            if int(a or b) not in self.prices:
                return False
        return True
