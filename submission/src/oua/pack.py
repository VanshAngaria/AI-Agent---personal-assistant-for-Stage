"""Read-only access to the supplied data pack. Only the pack is evidence about viewers and titles."""

from __future__ import annotations

import csv
import json
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

from oua.catalogue import Catalogue
from oua.policy import Policy
from oua.store import Store
from oua.timeutil import UTC, parse_ts


def read_jsonl(path: Path) -> Iterator[dict[str, Any]]:
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                yield json.loads(line)


@dataclass
class Pack:
    root: Path
    goals: dict[str, Any]
    interventions: dict[str, Any]
    pricing: dict[str, Any]
    catalogue: Catalogue
    policy: Policy

    @classmethod
    def open(cls, root: Path, policy: Policy | None = None) -> "Pack":
        root = Path(root)
        load = lambda p: json.loads((root / p).read_text(encoding="utf-8"))  # noqa: E731
        policy = policy or Policy.load()
        catalogue = Catalogue.load(root / "data" / "catalogue.csv", root / "data" / "episodes.csv")
        catalogue.screen(policy.safety["instruction_patterns"])
        return cls(root, load("config/goals.json"), load("config/interventions.json"),
                   load("config/pricing.json"), catalogue, policy)

    def path(self, rel: str) -> Path:
        return self.root / rel

    def history_events(self) -> Iterator[dict[str, Any]]:
        return read_jsonl(self.path("data/events.jsonl"))

    def profile_rows(self) -> Iterator[dict[str, str]]:
        with open(self.path("data/profiles_snapshot.csv"), encoding="utf-8") as f:
            yield from csv.DictReader(f)

    def arms(self) -> Iterator[tuple[str, str]]:
        with open(self.path("replay/arms.csv"), encoding="utf-8") as f:
            for r in csv.DictReader(f):
                yield r["user_id"], r["arm"]

    def triggers(self) -> list[dict[str, Any]]:
        return list(read_jsonl(self.path("replay/triggers.jsonl")))

    def live_events(self) -> list[dict[str, Any]]:
        return list(read_jsonl(self.path("replay/live_events.jsonl")))

    def control_events(self) -> list[dict[str, Any]]:
        return list(read_jsonl(self.path("replay/control_events.jsonl")))

    def model_faults(self) -> list[dict[str, Any]]:
        p = self.path("replay/model_faults.jsonl")
        return list(read_jsonl(p)) if p.exists() else []


def load_pack_into_store(pack: Pack, store: Store) -> dict[str, Any]:
    """Load history, profiles and arms into the user data store. Returns load statistics."""
    lines, redelivered = store.ingest_events_bulk(pack.history_events(), source="history")
    profiles = store.load_profiles(pack.profile_rows())
    arms = store.load_assignments(pack.arms())
    # Popularity evidence for "popular_in_dialect" rows: observed plays per content in the history.
    plays: Counter = Counter()
    for row in store.conn.execute("SELECT properties FROM events WHERE event = 'play_started'"):
        t = pack.catalogue.get(json.loads(row["properties"]).get("title_id"))
        if t:
            plays[t.content_slug] += 1
    pack.catalogue.plays_by_slug = plays
    first, last = store.conn.execute("SELECT MIN(received_at), MAX(received_at) FROM events").fetchone()
    return {
        "history_lines": lines,
        "history_redeliveries": redelivered,
        "history_first_received_at": first,
        "history_last_received_at": last,
        "profile_rows": profiles,
        "arms": arms,
    }


def history_window_start(store: Store) -> datetime:
    """Start of the observed history. With no history at all, nothing about the past can be proven."""
    value = store.conn.execute("SELECT MIN(received_at) FROM events WHERE source = 'history'").fetchone()[0]
    return parse_ts(value) if value else datetime.max.replace(tzinfo=UTC)
