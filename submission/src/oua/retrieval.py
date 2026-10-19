"""Retrieval for the viewer (the "R" in RAG): BM25 over the catalogue, queried with the viewer's own signals.

The query is built from what this viewer searched for and the titles they played (their names, genres and plot text),
each term weighted by its source. Hits are filtered to titles the viewer can actually be shown (their app dialect,
available now, not quarantined) and carry the matched terms and the source events, so every retrieved title can be
cited. Retrieval ranks; it never decides eligibility. Pure Python, no network: the pack never leaves the machine.
"""

from __future__ import annotations

import math
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from oua.catalogue import Catalogue, Title
from oua.reconcile import ViewerContext
from oua.timeutil import parse_ts

_TOKEN = re.compile(r"[a-z0-9]+")
STOP = frozenset("a an the and or of in on at to for with by from is are was be his her their its into over gets get set "
                 "two one who what when where this that film show micro drama".split())


def tokens(text: str) -> list[str]:
    return [t for t in _TOKEN.findall((text or "").lower()) if len(t) > 2 and t not in STOP]


@dataclass
class Hit:
    title_id: str
    score: float
    matched: list[str]
    sources: list[str]

    def as_dict(self) -> dict[str, Any]:
        return {"title_id": self.title_id, "score": round(self.score, 3), "matched": self.matched, "sources": self.sources}


@dataclass
class InterestQuery:
    terms: Counter = field(default_factory=Counter)
    term_sources: dict[str, set[str]] = field(default_factory=lambda: defaultdict(set))
    searches: list[str] = field(default_factory=list)
    watched: list[str] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not self.terms

    def top_terms(self, n: int = 12) -> list[str]:
        return [t for t, _ in self.terms.most_common(n)]


class CatalogueIndex:
    def __init__(self, catalogue: Catalogue, field_boost: dict[str, float], k1: float = 1.2, b: float = 0.75) -> None:
        self.cat = catalogue
        self.k1, self.b = k1, b
        self.tf: dict[str, Counter] = {}
        self.len: dict[str, float] = {}
        df: Counter = Counter()
        for t in catalogue.titles.values():
            c: Counter = Counter()
            for name, text in (("title", t.title), ("genres", " ".join(t.genres)), ("content_type", t.content_type.replace("_", " ")),
                               ("description", t.description)):
                for tok in tokens(text):
                    c[tok] += field_boost.get(name, 1.0)
            self.tf[t.title_id] = c
            self.len[t.title_id] = sum(c.values())
            df.update(c.keys())
        n = max(1, len(self.tf))
        self.avg = sum(self.len.values()) / n
        self.idf = {term: math.log(1 + (n - d + 0.5) / (d + 0.5)) for term, d in df.items()}

    def score(self, title_id: str, query: Counter) -> tuple[float, list[str]]:
        tf, dl = self.tf[title_id], self.len[title_id]
        s, matched = 0.0, []
        for term, qw in query.items():
            f = tf.get(term, 0.0)
            if not f:
                continue
            idf = self.idf.get(term, 0.0)
            s += qw * idf * (f * (self.k1 + 1)) / (f + self.k1 * (1 - self.b + self.b * dl / self.avg))
            matched.append(term)
        return s, matched

    def search(self, query: InterestQuery, allowed: list[Title], exclude_slugs: set[str], top_k: int,
               min_score: float = 0.0) -> list[Hit]:
        if query.empty:
            return []
        hits = []
        for t in allowed:
            if t.content_slug in exclude_slugs:
                continue
            s, matched = self.score(t.title_id, query.terms)
            if s > max(0.0, min_score):
                srcs = sorted({src for m in matched for src in query.term_sources.get(m, ())})[:6]
                hits.append(Hit(t.title_id, s, sorted(matched, key=lambda m: -query.terms[m])[:8], srcs))
        hits.sort(key=lambda h: (-h.score, h.title_id))
        return hits[:top_k]


def interest_query(ctx: ViewerContext, cat: Catalogue, policy_retrieval: dict[str, Any]) -> InterestQuery:
    """Weighted terms from this viewer's searches and played titles, each term remembering which event it came from."""
    q = InterestQuery()
    w = policy_retrieval.get("weights", {})
    s_cut = ctx.known_at - timedelta(days=policy_retrieval.get("search_lookback_days", 30))
    p_cut = ctx.known_at - timedelta(days=policy_retrieval.get("watch_lookback_days", 30))
    for e in ctx.events:
        at = parse_ts(e["received_at"])
        props = e.get("properties") or {}
        if e["event"] == "search" and at >= s_cut:
            text = str(props.get("query") or "")
            q.searches.append(text)
            for tok in tokens(text):
                q.terms[tok] += w.get("search_term", 2.0)
                q.term_sources[tok].add(f"event:{e['event_id']}")
        elif e["event"] == "play_started" and at >= p_cut:
            t = cat.get(props.get("title_id"))
            if t is None:
                continue
            q.watched.append(t.title_id)
            for tok in set(tokens(f"{t.title} {' '.join(t.genres)} {t.description}")):
                q.terms[tok] += w.get("watched_title_term", 1.0)
                q.term_sources[tok].add(f"event:{e['event_id']}")
    return q
