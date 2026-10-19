"""Runtime configuration. The private data pack stays outside this package."""

from __future__ import annotations

import os
from pathlib import Path

from pydantic import BaseModel, Field, field_validator

SUBMISSION_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DB_PATH = SUBMISSION_ROOT / "viewer_state" / "agent.sqlite"
DEFAULT_GOALS_PATH = SUBMISSION_ROOT / "config" / "goals.json"


class Settings(BaseModel):
    """Process settings. Viewer/title evidence is read only from pack_path later."""

    pack_path: Path | None = Field(
        default=None,
        description="Local path to the supplied data pack. Not required for foundation tests.",
    )
    db_path: Path = Field(default=DEFAULT_DB_PATH)
    goals_path: Path = Field(default=DEFAULT_GOALS_PATH)
    decision_deadline_ms: int = 60_000
    max_model_calls: int = 8
    max_tool_calls: int = 20

    @field_validator("pack_path", mode="before")
    @classmethod
    def _empty_pack_to_none(cls, value: object) -> object:
        if value is None or value == "":
            return None
        return Path(str(value)).expanduser().resolve()

    @field_validator("db_path", "goals_path", mode="before")
    @classmethod
    def _expand_path(cls, value: object) -> object:
        return Path(str(value)).expanduser()

    @classmethod
    def from_env(cls) -> "Settings":
        kwargs: dict[str, object] = {}
        if os.environ.get("OUA_PACK_PATH"):
            kwargs["pack_path"] = os.environ["OUA_PACK_PATH"]
        if os.environ.get("OUA_DB_PATH"):
            kwargs["db_path"] = os.environ["OUA_DB_PATH"]
        if os.environ.get("OUA_GOALS_PATH"):
            kwargs["goals_path"] = os.environ["OUA_GOALS_PATH"]
        return cls(**kwargs)
