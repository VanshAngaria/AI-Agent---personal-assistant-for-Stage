"""SQLite persistence for viewer state. No ingestion or planning logic."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from oua.schemas import Decision, ExperimentArm


class DuplicateActionError(ValueError):
    pass


class UniqueActivePlanError(ValueError):
    pass


SCHEMA_SQL = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS experiment_assignments (
    viewer_id TEXT PRIMARY KEY,
    arm TEXT NOT NULL CHECK (arm IN ('agent', 'holdout')),
    assigned_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS processed_triggers (
    event_id TEXT PRIMARY KEY,
    viewer_id TEXT NOT NULL,
    first_deliver_at TEXT NOT NULL,
    decision_id TEXT
);

CREATE TABLE IF NOT EXISTS facts (
    fact_id INTEGER PRIMARY KEY AUTOINCREMENT,
    viewer_id TEXT NOT NULL,
    fact_key TEXT NOT NULL,
    value_json TEXT NOT NULL,
    source TEXT NOT NULL,
    source_id TEXT,
    at TEXT,
    recorded_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_facts_viewer_key ON facts (viewer_id, fact_key);

CREATE TABLE IF NOT EXISTS active_plans (
    viewer_id TEXT PRIMARY KEY,
    goal_id TEXT NOT NULL,
    deadline TEXT NOT NULL,
    success_event TEXT NOT NULL,
    success_condition TEXT NOT NULL,
    window_hours INTEGER NOT NULL,
    rule_version TEXT NOT NULL,
    started_at TEXT NOT NULL,
    decision_id TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS goal_rule_versions (
    goal_id TEXT NOT NULL,
    version TEXT NOT NULL,
    spec_json TEXT NOT NULL,
    effective_at TEXT NOT NULL,
    PRIMARY KEY (goal_id, version)
);

CREATE TABLE IF NOT EXISTS decisions (
    decision_id TEXT PRIMARY KEY,
    viewer_id TEXT NOT NULL,
    trigger_event_id TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('PUBLISHED', 'ABSTAINED', 'FAILED_SAFE')),
    action TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    decided_in_ms INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (trigger_event_id)
);

CREATE TABLE IF NOT EXISTS decision_snapshots (
    decision_id TEXT PRIMARY KEY,
    viewer_id TEXT NOT NULL,
    facts_json TEXT NOT NULL,
    FOREIGN KEY (decision_id) REFERENCES decisions(decision_id)
);

CREATE TABLE IF NOT EXISTS actions (
    action_id TEXT PRIMARY KEY,
    decision_id TEXT NOT NULL,
    viewer_id TEXT NOT NULL,
    trigger_event_id TEXT NOT NULL,
    type TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (decision_id) REFERENCES decisions(decision_id),
    UNIQUE (decision_id, type),
    UNIQUE (viewer_id, trigger_event_id, type)
);

CREATE TABLE IF NOT EXISTS plan_revisions (
    revision_id INTEGER PRIMARY KEY AUTOINCREMENT,
    viewer_id TEXT NOT NULL,
    previous_goal_id TEXT,
    new_goal_id TEXT,
    previous_success_event TEXT,
    new_success_event TEXT,
    previous_rule_version TEXT,
    new_rule_version TEXT,
    reason TEXT NOT NULL,
    at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS outcomes (
    outcome_id INTEGER PRIMARY KEY AUTOINCREMENT,
    viewer_id TEXT NOT NULL,
    goal_id TEXT NOT NULL,
    rule_version TEXT NOT NULL,
    started_at TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('succeeded', 'failed', 'inconclusive')),
    signal_event_id TEXT,
    judged_at TEXT NOT NULL,
    UNIQUE (viewer_id, goal_id, started_at)
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


class Store:
    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path)
        self.conn = _connect(self.db_path)
        self.conn.executescript(SCHEMA_SQL)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def set_assignment(self, viewer_id: str, arm: ExperimentArm | str) -> None:
        value = arm.value if isinstance(arm, ExperimentArm) else arm
        if value not in ("agent", "holdout"):
            raise ValueError(f"unsupported arm {value!r}")
        self.conn.execute(
            """
            INSERT INTO experiment_assignments (viewer_id, arm, assigned_at)
            VALUES (?, ?, ?)
            ON CONFLICT(viewer_id) DO UPDATE SET arm = excluded.arm
            """,
            (viewer_id, value, _now()),
        )
        self.conn.commit()

    def get_assignment(self, viewer_id: str) -> str | None:
        row = self.conn.execute(
            "SELECT arm FROM experiment_assignments WHERE viewer_id = ?",
            (viewer_id,),
        ).fetchone()
        return None if row is None else str(row["arm"])

    def register_goal_rule(self, goal_id: str, version: str, spec: dict[str, Any], effective_at: str) -> None:
        self.conn.execute(
            """
            INSERT INTO goal_rule_versions (goal_id, version, spec_json, effective_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(goal_id, version) DO UPDATE SET spec_json = excluded.spec_json
            """,
            (goal_id, version, json.dumps(spec, ensure_ascii=False), effective_at),
        )
        self.conn.commit()

    def add_fact(
        self,
        viewer_id: str,
        fact_key: str,
        value: Any,
        source: str,
        source_id: str | None = None,
        at: str | None = None,
    ) -> int:
        cur = self.conn.execute(
            """
            INSERT INTO facts (viewer_id, fact_key, value_json, source, source_id, at, recorded_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                viewer_id,
                fact_key,
                json.dumps(value, ensure_ascii=False),
                source,
                source_id,
                at,
                _now(),
            ),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def list_facts(self, viewer_id: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM facts WHERE viewer_id = ? ORDER BY fact_id",
            (viewer_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    def claim_trigger(self, event_id: str, viewer_id: str, deliver_at: str) -> bool:
        """Return True if this is the first time the trigger is seen."""
        try:
            self.conn.execute(
                """
                INSERT INTO processed_triggers (event_id, viewer_id, first_deliver_at, decision_id)
                VALUES (?, ?, ?, NULL)
                """,
                (event_id, viewer_id, deliver_at),
            )
            self.conn.commit()
            return True
        except sqlite3.IntegrityError:
            return False

    def bind_trigger_decision(self, event_id: str, decision_id: str) -> None:
        self.conn.execute(
            "UPDATE processed_triggers SET decision_id = ? WHERE event_id = ?",
            (decision_id, event_id),
        )
        self.conn.commit()

    def get_decision_id_for_trigger(self, event_id: str) -> str | None:
        row = self.conn.execute(
            "SELECT decision_id FROM processed_triggers WHERE event_id = ?",
            (event_id,),
        ).fetchone()
        if row is None or row["decision_id"] is None:
            return None
        return str(row["decision_id"])

    def set_active_plan(
        self,
        viewer_id: str,
        goal_id: str,
        deadline: str,
        success_event: str,
        success_condition: str,
        window_hours: int,
        rule_version: str,
        started_at: str,
        decision_id: str,
        replace_existing: bool = False,
    ) -> None:
        existing = self.conn.execute(
            "SELECT goal_id, decision_id FROM active_plans WHERE viewer_id = ?",
            (viewer_id,),
        ).fetchone()
        if existing is not None and not replace_existing:
            raise UniqueActivePlanError(
                f"viewer {viewer_id} already has active goal {existing['goal_id']}"
            )
        self.conn.execute(
            """
            INSERT INTO active_plans (
                viewer_id, goal_id, deadline, success_event, success_condition,
                window_hours, rule_version, started_at, decision_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(viewer_id) DO UPDATE SET
                goal_id = excluded.goal_id,
                deadline = excluded.deadline,
                success_event = excluded.success_event,
                success_condition = excluded.success_condition,
                window_hours = excluded.window_hours,
                rule_version = excluded.rule_version,
                started_at = excluded.started_at,
                decision_id = excluded.decision_id
            """,
            (
                viewer_id,
                goal_id,
                deadline,
                success_event,
                success_condition,
                window_hours,
                rule_version,
                started_at,
                decision_id,
            ),
        )
        self.conn.commit()

    def get_active_plan(self, viewer_id: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM active_plans WHERE viewer_id = ?",
            (viewer_id,),
        ).fetchone()
        return None if row is None else dict(row)

    def clear_active_plan(self, viewer_id: str) -> None:
        self.conn.execute("DELETE FROM active_plans WHERE viewer_id = ?", (viewer_id,))
        self.conn.commit()

    def save_decision(self, decision: Decision, snapshot_facts: list[dict[str, Any]] | None = None) -> None:
        payload = json.dumps(decision.to_runtime_dict(), ensure_ascii=False)
        trigger_id = decision.trigger.event_id
        try:
            self.conn.execute(
                """
                INSERT INTO decisions (
                    decision_id, viewer_id, trigger_event_id, status, action,
                    payload_json, decided_in_ms, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    decision.decision_id,
                    decision.viewer_id,
                    trigger_id,
                    decision.status.value,
                    decision.action.value,
                    payload,
                    decision.decided_in_ms,
                    _now(),
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise ValueError(
                f"decision {decision.decision_id} or trigger {trigger_id} already stored"
            ) from exc
        facts = snapshot_facts if snapshot_facts is not None else self.list_facts(decision.viewer_id)
        self.conn.execute(
            """
            INSERT INTO decision_snapshots (decision_id, viewer_id, facts_json)
            VALUES (?, ?, ?)
            """,
            (decision.decision_id, decision.viewer_id, json.dumps(facts, ensure_ascii=False)),
        )
        self.bind_trigger_decision(trigger_id, decision.decision_id)
        self.conn.commit()

    def get_decision(self, decision_id: str) -> Decision | None:
        row = self.conn.execute(
            "SELECT payload_json FROM decisions WHERE decision_id = ?",
            (decision_id,),
        ).fetchone()
        if row is None:
            return None
        return Decision.model_validate(json.loads(row["payload_json"]))

    def get_snapshot(self, decision_id: str) -> list[dict[str, Any]] | None:
        row = self.conn.execute(
            "SELECT facts_json FROM decision_snapshots WHERE decision_id = ?",
            (decision_id,),
        ).fetchone()
        if row is None:
            return None
        return json.loads(row["facts_json"])

    def save_action(
        self,
        action_id: str,
        decision_id: str,
        viewer_id: str,
        trigger_event_id: str,
        action_type: str,
        payload: dict[str, Any],
    ) -> None:
        try:
            self.conn.execute(
                """
                INSERT INTO actions (
                    action_id, decision_id, viewer_id, trigger_event_id, type, payload_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    action_id,
                    decision_id,
                    viewer_id,
                    trigger_event_id,
                    action_type,
                    json.dumps(payload, ensure_ascii=False),
                    _now(),
                ),
            )
            self.conn.commit()
        except sqlite3.IntegrityError as exc:
            raise DuplicateActionError(
                f"duplicate action {action_id} or ({viewer_id}, {trigger_event_id}, {action_type})"
            ) from exc

    def save_outcome(
        self,
        viewer_id: str,
        goal_id: str,
        rule_version: str,
        started_at: str,
        status: str,
        signal_event_id: str | None,
        judged_at: str,
    ) -> None:
        if status not in ("succeeded", "failed", "inconclusive"):
            raise ValueError(f"invalid outcome status {status!r}")
        self.conn.execute(
            """
            INSERT INTO outcomes (
                viewer_id, goal_id, rule_version, started_at, status, signal_event_id, judged_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (viewer_id, goal_id, rule_version, started_at, status, signal_event_id, judged_at),
        )
        self.conn.commit()

    def list_active_plans_for_goal(self, goal_id: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM active_plans WHERE goal_id = ?",
            (goal_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    def add_plan_revision(
        self,
        viewer_id: str,
        reason: str,
        *,
        previous_goal_id: str | None = None,
        new_goal_id: str | None = None,
        previous_success_event: str | None = None,
        new_success_event: str | None = None,
        previous_rule_version: str | None = None,
        new_rule_version: str | None = None,
        at: str | None = None,
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO plan_revisions (
                viewer_id, previous_goal_id, new_goal_id, previous_success_event,
                new_success_event, previous_rule_version, new_rule_version, reason, at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                viewer_id,
                previous_goal_id,
                new_goal_id,
                previous_success_event,
                new_success_event,
                previous_rule_version,
                new_rule_version,
                reason,
                at or _now(),
            ),
        )
        self.conn.commit()

    def list_plan_revisions(self, viewer_id: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM plan_revisions WHERE viewer_id = ? ORDER BY revision_id",
            (viewer_id,),
        ).fetchall()
        return [dict(row) for row in rows]
