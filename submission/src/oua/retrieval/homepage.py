"""Assemble homepage rows that the platform will actually show."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from oua.retrieval.catalogue import CatalogueIndex, RetrieveQuery
from oua.retrieval.policy import PolicyIndex


class HomepageRow(BaseModel):
    model_config = ConfigDict(extra="forbid")

    row_type: str
    title_ids: list[str]


class HomepagePage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    dialect: str
    hero_title_id: str | None = None
    rows: list[HomepageRow] = Field(default_factory=list)
    dropped_rows: list[str] = Field(default_factory=list)
    filters: list[str] = Field(default_factory=list)


class HomepageAssembler:
    def __init__(self, catalogue: CatalogueIndex, policy: PolicyIndex) -> None:
        self.catalogue = catalogue
        self.policy = policy

    def assemble(
        self,
        *,
        dialect: str,
        at: datetime,
        allow_premium: bool,
        query_text: str | None = None,
        unfinished_slug: str | None = None,
        watched_genres: list[str] | None = None,
    ) -> HomepagePage | None:
        if dialect not in {item.dialect for item in self.catalogue.titles.values()}:
            return None
        min_titles = self.policy.home_min_titles
        max_titles = self.policy.home_max_titles
        max_rows = self.policy.home_max_rows
        seen_slugs: set[str] = set()
        page = HomepagePage(dialect=dialect, filters=[f"dialect={dialect}"])

        def take(candidates: list, limit: int) -> list[str]:
            ids: list[str] = []
            for item in candidates:
                if item.content_slug in seen_slugs:
                    continue
                ids.append(item.title_id)
                seen_slugs.add(item.content_slug)
                if len(ids) >= limit:
                    break
            return ids

        row_specs: list[tuple[str, RetrieveQuery]] = []
        if unfinished_slug:
            row_specs.append(
                (
                    "next_episode",
                    RetrieveQuery(
                        dialect=dialect,
                        at=at,
                        allow_premium=allow_premium,
                        content_slug=unfinished_slug,
                    ),
                )
            )
        if watched_genres:
            row_specs.append(
                (
                    "because_you_watched",
                    RetrieveQuery(
                        dialect=dialect,
                        at=at,
                        allow_premium=allow_premium,
                        genres=watched_genres[:1],
                    ),
                )
            )
        row_specs.append(
            (
                "new_releases",
                RetrieveQuery(
                    dialect=dialect,
                    at=at,
                    allow_premium=allow_premium,
                    released_within_days=30,
                ),
            )
        )
        row_specs.append(
            (
                "popular_in_dialect",
                RetrieveQuery(dialect=dialect, at=at, allow_premium=allow_premium, query_text=query_text),
            )
        )

        for row_type, query in row_specs:
            if len(page.rows) >= max_rows:
                break
            ids = take(self.catalogue.retrieve(query), max_titles)
            if len(ids) < min_titles:
                page.dropped_rows.append(f"{row_type}:below_minimum")
                continue
            page.rows.append(HomepageRow(row_type=row_type, title_ids=ids))

        if not page.rows:
            return None
        hero_pool = [title_id for row in page.rows for title_id in row.title_ids]
        page.hero_title_id = hero_pool[0] if hero_pool else None
        return page
