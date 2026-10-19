"""Viewer snapshot used by the goal selector. Built from facts, not from guessed history."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class SeriesProgress(BaseModel):
    model_config = ConfigDict(extra="forbid")

    content_slug: str
    content_type: str
    last_play_at: datetime
    last_play_event_id: str
    episode_count: int
    completed_episode_ids: list[str] = Field(default_factory=list)

    @property
    def unfinished(self) -> bool:
        if self.content_type not in {"show", "micro_drama"}:
            return False
        if self.episode_count <= 1:
            return False
        return len(set(self.completed_episode_ids)) < self.episode_count


class TimedEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_id: str
    received_at: datetime
    title_id: str | None = None
    content_slug: str | None = None


class ViewerSnapshot(BaseModel):
    """Known-as-of decision_time. Missing fields stay None; callers must not invent them."""

    model_config = ConfigDict(extra="forbid")

    viewer_id: str
    decision_time: datetime
    trigger_event_id: str
    trigger_event: str
    app_dialect: str | None = None
    dialect_source: str | None = None
    signup_at: datetime | None = None
    signup_evidence_id: str | None = None
    subscription_status: str | None = None
    status_evidence_id: str | None = None
    has_paid: bool = False
    paid_evidence_id: str | None = None
    play_started: list[TimedEvent] = Field(default_factory=list)
    episode_completed: list[TimedEvent] = Field(default_factory=list)
    app_opens: list[TimedEvent] = Field(default_factory=list)
    series: list[SeriesProgress] = Field(default_factory=list)
