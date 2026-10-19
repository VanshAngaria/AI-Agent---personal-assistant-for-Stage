"""Catalogue retrieval. Every result is a real title_id; descriptions are content, never instructions."""

from __future__ import annotations

import csv
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path

from oua.timeutil import parse_ts

CATALOGUE_DIALECTS = frozenset({"d1", "d2", "d3", "d4", "d5", "d6"})

@dataclass
class Title:
    title_id: str
    content_slug: str
    title: str
    dialect: str
    language: str
    content_type: str
    genres: list[str]
    release_date: date
    available_from: datetime
    available_until: datetime | None
    is_premium: bool
    description: str
    withdrawn_at: datetime | None = None
    quarantine_reason: str | None = None  # set by Catalogue.screen(); quarantined titles are never promoted

    @property
    def instruction_like(self) -> bool:
        return self.quarantine_reason is not None


@dataclass
class Episode:
    episode_id: str
    content_slug: str
    season: int
    episode: int
    minutes: int


@dataclass
class Catalogue:
    titles: dict[str, Title]
    episodes: dict[str, list[Episode]]
    plays_by_slug: Counter = field(default_factory=Counter)
    withdrawals: list[dict] = field(default_factory=list)

    @classmethod
    def load(cls, catalogue_csv: Path, episodes_csv: Path) -> "Catalogue":
        titles: dict[str, Title] = {}
        with open(catalogue_csv, encoding="utf-8") as f:
            for r in csv.DictReader(f):
                titles[r["title_id"]] = Title(
                    title_id=r["title_id"],
                    content_slug=r["content_slug"],
                    title=r["title"],
                    dialect=r["dialect"],
                    language=r["language"],
                    content_type=r["content_type"],
                    genres=[g for g in (r["genres"] or "").split("|") if g],
                    release_date=date.fromisoformat(r["release_date"]),
                    available_from=parse_ts(r["available_from"]),
                    available_until=parse_ts(r["available_until"]),
                    is_premium=(r["is_premium"] or "").lower() == "true",
                    description=r["description"] or "",
                )
        episodes: dict[str, list[Episode]] = {}
        with open(episodes_csv, encoding="utf-8") as f:
            for r in csv.DictReader(f):
                ep = Episode(r["episode_id"], r["content_slug"], int(r["season"]), int(r["episode"]), int(r["minutes"]))
                episodes.setdefault(ep.content_slug, []).append(ep)
        for eps in episodes.values():
            eps.sort(key=lambda e: (e.season, e.episode))
        return cls(titles=titles, episodes=episodes)

    def screen(self, patterns: list[str]) -> list[str]:
        """Quarantine titles whose text reads like an instruction to an automated agent (patterns come from policy).

        The text is never executed either way; quarantine only stops the agent promoting the title until a human looks.
        """
        compiled = [re.compile(p, re.IGNORECASE) for p in patterns]
        flagged = []
        for t in self.titles.values():
            for rx in compiled:
                m = rx.search(f"{t.title} {t.description}")
                if m:
                    t.quarantine_reason = f"matched pattern {rx.pattern!r}: {m.group(0)!r}"
                    flagged.append(t.title_id)
                    break
        return flagged

    # -- facts about titles -------------------------------------------------------------------------------

    def get(self, title_id: str | None) -> Title | None:
        return self.titles.get(title_id or "")

    def available(self, title: Title, at: datetime) -> bool:
        if title.withdrawn_at is not None and at >= title.withdrawn_at:
            return False
        if at < title.available_from:
            return False
        return title.available_until is None or at < title.available_until

    def withdraw(self, title_id: str, at: datetime, reason: str) -> bool:
        title = self.titles.get(title_id)
        if title is None:
            return False
        title.withdrawn_at = at if title.withdrawn_at is None else min(title.withdrawn_at, at)
        self.withdrawals.append({"title_id": title_id, "at": at, "reason": reason})
        return True

    def episode_index(self, content_slug: str) -> dict[str, int]:
        return {ep.episode_id: i for i, ep in enumerate(self.episodes.get(content_slug, []))}

    def episode_count(self, content_slug: str) -> int:
        return len(self.episodes.get(content_slug, []))

    def is_new_release(self, title: Title, at: datetime, within_days: int = 30) -> bool:  # window comes from policy
        day = at.date()
        return day - timedelta(days=within_days) <= title.release_date <= day

    # -- retrieval ----------------------------------------------------------------------------------------

    def promotable(self, dialect: str, at: datetime) -> list[Title]:
        """Titles that may be promoted to a viewer whose app is set to `dialect` at time `at`."""
        if dialect not in CATALOGUE_DIALECTS:
            return []
        return [
            t
            for t in self.titles.values()
            if t.dialect == dialect and self.available(t, at) and not t.instruction_like
        ]

    def ranked(self, candidates: list[Title]) -> list[Title]:
        """Order by observed historical plays of the content (evidence), then newest release, then id."""
        return sorted(
            candidates,
            key=lambda t: (-self.plays_by_slug.get(t.content_slug, 0), -t.release_date.toordinal(), t.title_id),
        )
