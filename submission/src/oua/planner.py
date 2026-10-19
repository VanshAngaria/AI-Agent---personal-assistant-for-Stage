"""Decide the intervention for a committed goal. Proposes; never publishes (the verifier is the only gate).

Flow (retrieval-augmented):
  1. Build candidate rows from the policy's row sources: titles the viewer can be shown (app dialect, available,
     not quarantined), ranked by retrieval against the viewer's own searches/viewing, by observed popularity, recency
     or series progress. Premium handling and the hero strategy also come from policy.
  2. Hand the candidates to the model ("compose_page"): it may pick, order and drop, but only within what it was given.
  3. Validate the composition against the candidates (grounding). Anything invented is removed; if nothing usable is
     left, the deterministic composition is used instead and the reason is recorded.
  4. Follow-up messages are only proposed where the policy defines one for the goal, consent exists, the send time is
     outside quiet hours and the viewer's message day is free; the model may decline one but never add one.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from oua.catalogue import Catalogue, Title
from oua.policy import Policy
from oua.reconcile import ViewerContext
from oua.retrieval import CatalogueIndex, Hit, InterestQuery, interest_query
from oua.timeutil import ist_at, ist_date, iso

ROW_KINDS = {"next_episode", "retrieved", "popular", "new_releases", "second_genre"}


@dataclass
class PageProposal:
    dialect: str
    hero_title_id: str | None
    rows: list[dict[str, Any]]
    rationale: list[str] = field(default_factory=list)

    def title_ids(self) -> list[str]:
        ids = [self.hero_title_id] if self.hero_title_id else []
        return ids + [t for r in self.rows for t in r["title_ids"]]


@dataclass
class MessageProposal:
    channel: str  # push | whatsapp
    send_at: datetime
    title_id: str
    text: dict[str, str] | None = None
    template_id: str | None = None
    variables: dict[str, str] | None = None
    deeplink: str | None = None
    copy_key: str = ""
    rationale: str = ""


@dataclass
class CandidateRow:
    source: str
    row_type: str
    why: str
    titles: list[Title]


@dataclass
class Candidates:
    dialect: str
    hero: list[tuple[Title, str]]
    rows: list[CandidateRow]
    query: InterestQuery
    hits: list[Hit]
    notes: list[str] = field(default_factory=list)

    def ids(self) -> set[str]:
        return {t.title_id for t, _ in self.hero} | {t.title_id for r in self.rows for t in r.titles}


class Planner:
    def __init__(self, catalogue: Catalogue, interventions: dict[str, Any], policy: Policy, index: CatalogueIndex) -> None:
        self.cat = catalogue
        self.iv = interventions
        self.policy = policy
        self.index = index
        for name, src in policy.row_sources.items():
            if src.get("kind", name) not in ROW_KINDS:
                raise ValueError(f"row source {name!r} has unknown kind {src.get('kind')!r}; known: {sorted(ROW_KINDS)}")

    # -- 1. candidates --------------------------------------------------------------------------------------------

    def candidates(self, ctx: ViewerContext, goal_id: str, params: dict[str, Any], at: datetime) -> Candidates | None:
        cfg = self.policy.intervention(goal_id)
        page = self.policy.page
        paying = ctx.status in ("active", "trial_active")
        allow_premium = cfg.get("premium") == "allowed" or paying
        finished = {s.content_slug for s in ctx.series if s.total_episodes and s.furthest_index >= s.total_episodes - 1
                    and (s.content_type != "film" or s.content_slug in ctx.completed_slugs)}
        pool = [t for t in self.cat.promotable(ctx.dialect, at) if (allow_premium or not t.is_premium) and t.content_slug not in finished]
        if not pool:
            return None
        ranked = self.cat.ranked(pool)
        notes = [f"{len(pool)} titles promotable in {ctx.dialect} at publish time"
                 + ("" if allow_premium else "; premium excluded (viewer is not paying and the goal is about viewing)")]
        rcfg = self.policy.retrieval
        query = interest_query(ctx, self.cat, rcfg) if rcfg.get("enabled", True) else InterestQuery()
        hits = (self.index.search(query, pool, ctx.watched_slugs, int(rcfg.get("top_k", 14)), float(rcfg.get("min_score", 0.0)))
                if not query.empty else [])
        if query.empty:
            notes.append("no searches or plays to retrieve from; retrieval skipped")
        else:
            notes.append(f"retrieval query from {len(query.searches)} search(es) and {len(query.watched)} play(s): "
                         + ", ".join(query.top_terms(8)))
        by_id = {t.title_id: t for t in pool}
        retrieved = [by_id[h.title_id] for h in hits if h.title_id in by_id]
        new_days = int(page.get("new_release_days", 30))
        unfinished_ids = {s.title_id for s in ctx.series if s.unfinished}
        g2 = ctx.top_genres[1] if len(ctx.top_genres) > 1 else None
        kinds = {
            "next_episode": [t for t in ranked if t.title_id in unfinished_ids],
            "retrieved": retrieved,
            "popular": ranked,
            "new_releases": [t for t in ranked if self.cat.is_new_release(t, at, new_days)],
            "second_genre": [t for t in ranked if g2 and g2 in t.genres and t.content_slug not in ctx.watched_slugs],
        }
        cap = int(page.get("candidates_per_row", 14))
        rows = []
        for name in cfg.get("rows", []):
            src = self.policy.row_sources[name]
            titles = kinds[src.get("kind", name)]
            for key, value in (src.get("filter") or {}).items():
                titles = [t for t in titles if getattr(t, key) == value]
            why = src.get("why", "")
            if name == "second_genre" and g2:
                why = f"{why} ({g2})"
            rows.append(CandidateRow(name, src["row_type"], why, titles[:cap]))
        hero = self._hero_candidates(ctx, cfg.get("hero", ["popular"]), params, by_id, ranked, retrieved, at, new_days)
        return Candidates(ctx.dialect, hero, rows, query, hits, notes)

    def _hero_candidates(self, ctx, strategies, params, by_id, ranked, retrieved, at, new_days) -> list[tuple[Title, str]]:
        out: list[tuple[Title, str]] = []

        def add(t: Title | None, why: str) -> None:
            if t is not None and all(t.title_id != o[0].title_id for o in out):
                out.append((t, why))

        for s in strategies:
            if s == "goal_series":
                add(by_id.get(params.get("title_id")), "the unfinished series this goal is about")
            elif s == "deeplink" and ctx.deeplink:
                add(by_id.get(ctx.deeplink.get("title_id")), "title in the campaign link the viewer arrived from")
            elif s == "retrieved" and retrieved:
                add(retrieved[0], "best match to this viewer's searches and viewing")
            elif s == "new_release":
                add(next((t for t in ranked if self.cat.is_new_release(t, at, new_days)), None), "most played recent release")
            elif s == "short_episodes":
                add(next((t for t in ranked if t.content_type == "micro_drama"), None), "short episodes: easiest first full episode")
            elif s == "popular" and ranked:
                add(ranked[0], "most played title in the dialect")
        return out[:5]

    # -- 2. model payload --------------------------------------------------------------------------------------------

    def compose_payload(self, ctx: ViewerContext, goal: dict[str, Any], cands: Candidates, follow_up_allowed: bool) -> dict[str, Any]:
        cap = int(self.policy.llm.get("max_description_chars", 140))
        scores = {h.title_id: h for h in cands.hits}

        def item(t: Title, why: str | None = None) -> dict[str, Any]:
            d = {"id": t.title_id, "content": t.content_slug, "title": t.title, "type": t.content_type,
                 "genres": t.genres[:3], "episodes": self.cat.episode_count(t.content_slug), "premium": t.is_premium,
                 "summary": _clean(t.description, cap)}
            if t.title_id in scores:
                d["retrieval_score"] = round(scores[t.title_id].score, 2)
                d["matched_terms"] = scores[t.title_id].matched[:5]
            if why:
                d["why"] = why
            return d

        return {
            "goal": {"id": goal["id"], "name": goal["name"], "success": goal["success_signal"]["judged_by"]},
            "viewer": {"dialect": ctx.dialect, "language": ctx.language, "status": ctx.status, "plays": len(ctx.plays),
                       "completions": len(ctx.completions), "top_genres": ctx.top_genres,
                       "interest_terms": cands.query.top_terms(10)},
            "hero_candidates": [item(t, why) for t, why in cands.hero],
            "rows": [{"row_type": r.row_type, "source": r.source, "why": r.why, "candidates": [item(t) for t in r.titles]}
                     for r in cands.rows if r.titles],
            "max_titles": int(self.policy.page.get("titles_per_row", 8)),
            "max_rows": int(self.policy.page.get("max_rows", 5)),
            "follow_up_allowed": follow_up_allowed,
        }

    # -- 3. composition: deterministic, or the model's after grounding checks -------------------------------------------

    def deterministic(self, cands: Candidates) -> PageProposal | None:
        from oua.llm import MockProvider  # the mock composes exactly the deterministic way

        payload = {"hero_candidates": [{"id": t.title_id, "content": t.content_slug} for t, _ in cands.hero],
                   "rows": [{"row_type": r.row_type, "candidates": [{"id": t.title_id, "content": t.content_slug} for t in r.titles]}
                            for r in cands.rows if r.titles],
                   "max_titles": int(self.policy.page.get("titles_per_row", 8)), "follow_up_allowed": True}
        data = MockProvider._compose(payload)
        page, _ = self.from_model(cands, data)
        if page is not None:
            page.rationale = self._rationale(cands, page, "rules")
        return page

    def from_model(self, cands: Candidates, data: dict[str, Any]) -> tuple[PageProposal | None, list[str]]:
        problems: list[str] = []
        allowed_by_type: dict[str, set[str]] = {}
        for r in cands.rows:
            allowed_by_type.setdefault(r.row_type, set()).update(t.title_id for t in r.titles)
        all_ids = set().union(*allowed_by_type.values()) if allowed_by_type else set()
        hero_ids = {t.title_id for t, _ in cands.hero}
        max_titles = int(self.policy.page.get("titles_per_row", 8))
        max_rows = int(self.policy.page.get("max_rows", 5))
        used: set[str] = set()
        hero = str(data.get("hero_title_id") or "") or None
        if hero and hero not in hero_ids | all_ids:
            problems.append(f"hero {hero} was not among the candidates; dropped")
            hero = None
        if hero:
            used.add(self.cat.get(hero).content_slug)
        rows = []
        for row in data.get("rows") or []:
            rt = str(row.get("row_type"))
            if rt not in allowed_by_type:
                problems.append(f"row type {rt!r} was not offered; dropped")
                continue
            ids = []
            for tid in row.get("title_ids") or []:
                if tid not in allowed_by_type[rt]:
                    problems.append(f"{tid} not among the {rt} candidates; removed")
                    continue
                slug = self.cat.get(tid).content_slug
                if slug in used:
                    continue
                used.add(slug)
                ids.append(tid)
                if len(ids) >= max_titles:
                    break
            if len(ids) >= 3 and len(rows) < max_rows:
                rows.append({"row_type": rt, "title_ids": ids})
            elif ids:
                problems.append(f"row {rt} kept fewer than 3 valid titles; dropped")
        if not rows and not hero:
            return None, problems + ["nothing usable left after grounding checks"]
        return PageProposal(cands.dialect, hero, rows), problems

    def _rationale(self, cands: Candidates, page: PageProposal, by: str) -> list[str]:
        out = list(cands.notes)
        hero_why = {t.title_id: w for t, w in cands.hero}
        if page.hero_title_id:
            out.append(f"hero {page.hero_title_id}: {hero_why.get(page.hero_title_id, 'chosen by the model from the candidates')}")
        whys = {r.row_type: r.why for r in cands.rows if r.titles}
        for r in page.rows:
            out.append(f"row {r['row_type']}: {whys.get(r['row_type'], '')}")
        out.append(f"page composed by {by}")
        return out

    def finalize(self, cands: Candidates, page: PageProposal, by: str) -> PageProposal:
        page.rationale = self._rationale(cands, page, by)
        return page

    # -- 4. follow-up message -----------------------------------------------------------------------------------------

    def follow_up_config(self, goal_id: str) -> dict[str, Any] | None:
        return self.policy.intervention(goal_id).get("follow_up")

    def plan_message(self, ctx: ViewerContext, goal_id: str, params: dict[str, Any], deadline: datetime,
                     page: PageProposal | None, message_days: set[str], lessons: list[dict[str, Any]],
                     plan_has_message: bool, at: datetime) -> tuple[MessageProposal | None, str]:
        fu = self.follow_up_config(goal_id)
        if not fu:
            return None, f"no message: the policy defines no follow-up for {goal_id}; it is pursued in-session"
        if plan_has_message:
            return None, "no message: this plan already scheduled its one follow-up"
        send_day = ist_date(at) + timedelta(days=int(fu.get("day_offset", 1)))
        send_at = ist_at(send_day, int(fu.get("send_hour_ist", 19)))
        if send_at >= deadline:
            return None, "no message: the next allowed send time is after the goal deadline"
        if str(send_day) in message_days:
            return None, f"no message: viewer already has a message on {send_day} (1/day cap)"
        lesson_kinds = {l["kind"] for l in lessons}
        channel = None
        for ch in fu.get("channels", ["push"]):
            consent = ctx.consent_push if ch == "push" else ctx.consent_whatsapp
            blocked = f"opted_out:{ch}" in lesson_kinds or (ch == "push" and "push_undeliverable" in lesson_kinds)
            if consent and not blocked:
                channel = ch
                break
        if channel is None:
            return None, "no message: no channel with consent (or the channel is known to fail for this viewer)"
        title_id = params.get("title_id") if fu.get("title_from") == "goal_series" else (page.hero_title_id if page else None)
        t = self.cat.get(title_id)
        if t is None or t.dialect != ctx.dialect or not self.cat.available(t, send_at) or t.instruction_like:
            return None, "no message: no title that is valid in the viewer's app dialect at send time"
        why = f"{channel} at {fu.get('send_hour_ist', 19)}:00 IST to {fu.get('why', 'bring the viewer back')}"
        if channel == "push":
            return MessageProposal("push", send_at, t.title_id, deeplink=f"ott://title/{t.title_id}",
                                   copy_key=fu.get("copy", "return"), rationale=why), "ok"
        return MessageProposal("whatsapp", send_at, t.title_id, template_id=self.policy.whatsapp_template_for_titles,
                               variables={"title_name": t.title, "dialect_name": t.dialect}, rationale=why), "ok"

    def copy_payload(self, m: MessageProposal, ctx: ViewerContext, goal: dict[str, Any]) -> dict[str, Any]:
        t = self.cat.get(m.title_id)
        lang = "en" if ctx.language == "en" else "hin"
        cp = self.iv["push"]["copy"]
        return {"goal": goal["name"], "title_name": t.title, "language": lang, "title_max": cp["title_max_chars"],
                "body_max": cp["body_max_chars"], "forbidden": ", ".join(cp["forbidden_phrases"]),
                "template_body": self.template_copy(m, ctx)["body"]}

    def template_copy(self, m: MessageProposal, ctx: ViewerContext) -> dict[str, str]:
        t = self.cat.get(m.title_id)
        lang = "en" if ctx.language == "en" else "hin"
        tpl = self.policy.copy_templates[m.copy_key][lang]
        return {"title": t.title[: self.iv["push"]["copy"]["title_max_chars"]], "body": tpl.format(title=t.title)}


def _clean(text: str, cap: int) -> str:
    """Untrusted catalogue text for a prompt: strip markup characters and cap the length."""
    s = " ".join((text or "").replace("<", " ").replace(">", " ").replace("{", " ").replace("}", " ").split())
    return s[:cap]
