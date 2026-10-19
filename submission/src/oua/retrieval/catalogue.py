"""Structured catalogue retrieval. Descriptions are untrusted content, never instructions."""

from __future__ import annotations

import csv
import re
from datetime import datetime
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from oua.retrieval.tfidf import TfidfIndex
from oua.timeutil import as_utc

CATALOGUE_DIALECTS = frozenset({"d1", "d2", "d3", "d4", "d5", "d6"})
INJECTION_PATTERN = re.compile(
    r"system note|ignore (your|previous) rules|send a whatsapp",
    re.IGNORECASE,
)


def parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


class TitleRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title_id: str
    content_slug: str
    title: str
    dialect: str
    language: str
    content_type: str
    genres: list[str]
    release_date: str
    available_from: datetime
    available_until: datetime | None
    is_premium: bool
    description: str
    withdrawn_at: datetime | None = None

    @property
    def untrusted_instruction_like(self) -> bool:
        return bool(INJECTION_PATTERN.search(self.description or ""))


class EpisodeRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    episode_id: str
    content_slug: str
    season: int
    episode: int
    minutes: int


class RetrievedTitle(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title_id: str
    content_slug: str
    dialect: str
    content_type: str
    is_premium: bool
    filters: list[str]
    relevance_reason: str
    untrusted_instruction_like: bool
    tfidf_score: float | None = None


class RetrieveQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")

    dialect: str
    at: datetime
    allow_premium: bool = True
    content_types: list[str] | None = None
    released_within_days: int | None = None
    genres: list[str] | None = None
    query_text: str | None = None
    content_slug: str | None = None


class CatalogueIndex:
    def __init__(
        self,
        titles: dict[str, TitleRecord],
        episodes: dict[str, list[EpisodeRecord]],
    ) -> None:
        self.titles = titles
        self.episodes = episodes
        self._ordered = list(titles.values())
        self._tfidf = TfidfIndex(
            [f"{item.title} {item.description}" for item in self._ordered]
        )

    @classmethod
    def load(cls, catalogue_csv: Path, episodes_csv: Path) -> "CatalogueIndex":
        titles: dict[str, TitleRecord] = {}
        with catalogue_csv.open(encoding="utf-8") as handle:
            for row in csv.DictReader(handle):
                until = parse_ts(row.get("available_until") or None)
                available_from = parse_ts(row["available_from"])
                if available_from is None:
                    raise ValueError(f"catalogue row {row['title_id']} missing available_from")
                titles[row["title_id"]] = TitleRecord(
                    title_id=row["title_id"],
                    content_slug=row["content_slug"],
                    title=row["title"],
                    dialect=row["dialect"],
                    language=row["language"],
                    content_type=row["content_type"],
                    genres=[part for part in (row.get("genres") or "").split("|") if part],
                    release_date=row["release_date"],
                    available_from=available_from,
                    available_until=until,
                    is_premium=row.get("is_premium", "").lower() == "true",
                    description=row.get("description") or "",
                )
        episodes: dict[str, list[EpisodeRecord]] = {}
        with episodes_csv.open(encoding="utf-8") as handle:
            for row in csv.DictReader(handle):
                record = EpisodeRecord(
                    episode_id=row["episode_id"],
                    content_slug=row["content_slug"],
                    season=int(row["season"]),
                    episode=int(row["episode"]),
                    minutes=int(row["minutes"]),
                )
                episodes.setdefault(record.content_slug, []).append(record)
        for items in episodes.values():
            items.sort(key=lambda item: (item.season, item.episode))
        return cls(titles, episodes)

    def withdraw(self, title_id: str, at: datetime) -> None:
        title = self.titles.get(title_id)
        if title is None:
            return
        title.withdrawn_at = as_utc(at)

    def available(self, title: TitleRecord, at: datetime) -> bool:
        moment = as_utc(at)
        if title.withdrawn_at is not None and moment >= as_utc(title.withdrawn_at):
            return False
        if moment < as_utc(title.available_from):
            return False
        if title.available_until is not None and moment >= as_utc(title.available_until):
            return False
        return True

    def episode_count(self, content_slug: str) -> int:
        return len(self.episodes.get(content_slug, []))

    def retrieve(self, query: RetrieveQuery) -> list[RetrievedTitle]:
        applied: list[str] = [f"dialect={query.dialect}", f"available_at={as_utc(query.at).isoformat()}"]
        if query.dialect not in CATALOGUE_DIALECTS:
            return []
        if not query.allow_premium:
            applied.append("exclude_premium")
        if query.content_types:
            applied.append("content_types=" + ",".join(query.content_types))
        if query.released_within_days is not None:
            applied.append(f"released_within_days={query.released_within_days}")
        if query.genres:
            applied.append("genres=" + ",".join(query.genres))
        if query.content_slug:
            applied.append(f"content_slug={query.content_slug}")

        hits: list[tuple[float, int, RetrievedTitle]] = []
        for index, title in enumerate(self._ordered):
            if title.dialect != query.dialect:
                continue
            if not self.available(title, query.at):
                continue
            if not query.allow_premium and title.is_premium:
                continue
            if query.content_types and title.content_type not in query.content_types:
                continue
            if query.content_slug and title.content_slug != query.content_slug:
                continue
            if query.genres and not set(query.genres).intersection(title.genres):
                continue
            if query.released_within_days is not None:
                try:
                    release = datetime.fromisoformat(title.release_date)
                except ValueError:
                    release = datetime.fromisoformat(title.release_date + "T00:00:00")
                if release.tzinfo is None:
                    from datetime import timezone

                    release = release.replace(tzinfo=timezone.utc)
                age_days = (as_utc(query.at) - as_utc(release)).total_seconds() / 86400.0
                if age_days < 0 or age_days > query.released_within_days:
                    continue
            score = None
            reason = "passed structured filters"
            if query.query_text:
                score = self._tfidf.score(query.query_text, index)
                reason = f"tfidf={score:.4f} after structured filters"
            hits.append(
                (
                    -(score or 0.0),
                    index,
                    RetrievedTitle(
                        title_id=title.title_id,
                        content_slug=title.content_slug,
                        dialect=title.dialect,
                        content_type=title.content_type,
                        is_premium=title.is_premium,
                        filters=list(applied),
                        relevance_reason=reason,
                        untrusted_instruction_like=title.untrusted_instruction_like,
                        tfidf_score=score,
                    ),
                )
            )
        hits.sort()
        return [item[2] for item in hits]
