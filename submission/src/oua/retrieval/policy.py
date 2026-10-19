"""Load intervention/policy records with stable IDs."""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel, ConfigDict


class PolicyRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    record_id: str
    kind: str
    payload: dict


class PolicyIndex:
    def __init__(self, version: str, records: list[PolicyRecord], raw: dict) -> None:
        self.version = version
        self.records = records
        self.raw = raw
        self._by_id = {item.record_id: item for item in records}

    @classmethod
    def load(cls, path: Path) -> "PolicyIndex":
        raw = json.loads(path.read_text(encoding="utf-8"))
        records = [
            PolicyRecord(record_id="home_page", kind="home_page", payload=raw["home_page"]),
            PolicyRecord(record_id="push", kind="push", payload=raw["push"]),
            PolicyRecord(record_id="whatsapp", kind="whatsapp", payload=raw["whatsapp"]),
            PolicyRecord(record_id="in_app_message", kind="in_app_message", payload=raw["in_app_message"]),
            PolicyRecord(record_id="no_action", kind="no_action", payload=raw["no_action"]),
        ]
        for template in raw["whatsapp"]["approved_templates"]:
            records.append(
                PolicyRecord(
                    record_id=template["template_id"],
                    kind="whatsapp_template",
                    payload=template,
                )
            )
        return cls(version=str(raw.get("version")), records=records, raw=raw)

    def get(self, record_id: str) -> PolicyRecord:
        try:
            return self._by_id[record_id]
        except KeyError as exc:
            raise KeyError(f"unknown policy record {record_id!r}") from exc

    @property
    def home_min_titles(self) -> int:
        return int(self.raw["home_page"]["rows"]["min_titles_per_row"])

    @property
    def home_max_titles(self) -> int:
        return int(self.raw["home_page"]["rows"]["max_titles_per_row"])

    @property
    def home_max_rows(self) -> int:
        return int(self.raw["home_page"]["rows"]["max_rows"])
