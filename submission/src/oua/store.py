"""Durable state in SQLite.

Two logical stores share one file:
  * the *user data store* (events, profile rows): pack data plus everything delivered during replay;
  * the *agent state* (plans, decisions, actions, facts, outcomes, lessons, rule versions, control log).
Invariants that must never break are enforced by the schema, not only by code:
  * one open plan per viewer                      (partial unique index ux_open_plan)
  * one push/WhatsApp per viewer per IST day       (partial unique index ux_message_day)
  * one decision per trigger event_id              (UNIQUE trigger_event_id)
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Iterator

SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;

CREATE TABLE IF NOT EXISTS events (
    event_id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    event TEXT NOT NULL,
    received_at TEXT NOT NULL,
    occurred_at TEXT,
    sent_at TEXT,
    session_id TEXT,
    app_dialect TEXT,
    app_language TEXT,
    platform TEXT,
    properties TEXT NOT NULL,
    source TEXT NOT NULL,
    deliveries INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS ix_events_user_time ON events (user_id, received_at);

CREATE TABLE IF NOT EXISTS profile_rows (
    row_id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL,
    payload TEXT NOT NULL,
    updated_at TEXT,
    snapshot_at TEXT
);
CREATE INDEX IF NOT EXISTS ix_profile_user ON profile_rows (user_id);

CREATE TABLE IF NOT EXISTS assignments (
    user_id TEXT PRIMARY KEY,
    arm TEXT NOT NULL CHECK (arm IN ('agent', 'holdout'))
);

CREATE TABLE IF NOT EXISTS processed_triggers (
    event_id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    first_deliver_at TEXT NOT NULL,
    deliveries INTEGER NOT NULL DEFAULT 1,
    last_deliver_at TEXT NOT NULL,
    decision_id TEXT
);

CREATE TABLE IF NOT EXISTS decisions (
    decision_id TEXT PRIMARY KEY,
    viewer_id TEXT NOT NULL,
    trigger_event_id TEXT NOT NULL UNIQUE,
    deliver_at TEXT NOT NULL,
    published_at TEXT,
    status TEXT NOT NULL,
    action TEXT NOT NULL,
    goal_id TEXT,
    payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_decisions_viewer ON decisions (viewer_id, deliver_at);

CREATE TABLE IF NOT EXISTS facts (
    fact_id INTEGER PRIMARY KEY AUTOINCREMENT,
    viewer_id TEXT NOT NULL,
    decision_id TEXT NOT NULL,
    fact_key TEXT NOT NULL,
    value TEXT NOT NULL,
    source TEXT NOT NULL,
    source_id TEXT,
    observed_at TEXT,
    known_at TEXT NOT NULL,
    freshness_s REAL
);
CREATE INDEX IF NOT EXISTS ix_facts_viewer ON facts (viewer_id, known_at);

CREATE TABLE IF NOT EXISTS actions (
    action_id TEXT PRIMARY KEY,
    decision_id TEXT NOT NULL REFERENCES decisions(decision_id),
    viewer_id TEXT NOT NULL,
    type TEXT NOT NULL,
    published_at TEXT NOT NULL,
    send_at TEXT,
    message_day TEXT,
    dialect TEXT,
    title_ids TEXT NOT NULL,
    payload TEXT NOT NULL,
    superseded_by TEXT,
    UNIQUE (decision_id, type)
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_message_day ON actions (viewer_id, message_day) WHERE message_day IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_actions_viewer ON actions (viewer_id, published_at);

CREATE TABLE IF NOT EXISTS plans (
    plan_id INTEGER PRIMARY KEY AUTOINCREMENT,
    viewer_id TEXT NOT NULL,
    arm TEXT NOT NULL,
    goal_id TEXT NOT NULL,
    rule_version TEXT NOT NULL,
    success_event TEXT NOT NULL,
    success_condition TEXT NOT NULL,
    window_hours INTEGER NOT NULL,
    params TEXT NOT NULL,
    started_at TEXT NOT NULL,
    deadline TEXT NOT NULL,
    decision_id TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('open', 'succeeded', 'failed', 'inconclusive', 'superseded')),
    closed_at TEXT,
    close_reason TEXT,
    signal_event_id TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_open_plan ON plans (viewer_id) WHERE status = 'open';

CREATE TABLE IF NOT EXISTS plan_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    plan_id INTEGER NOT NULL,
    viewer_id TEXT NOT NULL,
    at TEXT NOT NULL,
    change TEXT NOT NULL,
    detail TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS rule_versions (
    goal_id TEXT NOT NULL,
    version TEXT NOT NULL,
    success TEXT NOT NULL,
    effective_at TEXT NOT NULL,
    source TEXT NOT NULL,
    PRIMARY KEY (goal_id, version)
);

CREATE TABLE IF NOT EXISTS outcomes (
    plan_id INTEGER PRIMARY KEY REFERENCES plans(plan_id),
    viewer_id TEXT NOT NULL,
    arm TEXT NOT NULL,
    goal_id TEXT NOT NULL,
    rule_version TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('succeeded', 'failed', 'inconclusive')),
    reason TEXT NOT NULL,
    signal_event_id TEXT,
    signal_at TEXT,
    judged_at TEXT NOT NULL,
    judged_by TEXT NOT NULL,
    exposure TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS lessons (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    viewer_id TEXT NOT NULL,
    at TEXT NOT NULL,
    kind TEXT NOT NULL,
    detail TEXT NOT NULL,
    evidence TEXT NOT NULL,
    UNIQUE (viewer_id, kind, evidence)
);

CREATE TABLE IF NOT EXISTS control_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL,
    type TEXT NOT NULL,
    payload TEXT NOT NULL,
    affected TEXT NOT NULL
);
"""

EVENT_COLUMNS = (
    "event_id", "user_id", "event", "received_at", "occurred_at", "sent_at", "session_id",
    "app_dialect", "app_language", "platform",
)


def _row_to_event(row: sqlite3.Row) -> dict[str, Any]:
    out = {k: row[k] for k in EVENT_COLUMNS}
    out["properties"] = json.loads(row["properties"])
    out["source"] = row["source"]
    out["deliveries"] = row["deliveries"]
    return out


class Store:
    def __init__(self, db_path: Path | str) -> None:
        self.db_path = Path(db_path)
        if str(db_path) != ":memory:":
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(db_path), isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)

    def close(self) -> None:
        self.conn.close()

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        """One atomic unit of agent work (a decision with its actions, plan and facts)."""
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            yield self.conn
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise
        else:
            self.conn.execute("COMMIT")

    # -- user data store: events ---------------------------------------------------------------------------

    def ingest_event(self, e: dict[str, Any], source: str) -> bool:
        """Idempotent on event_id. Returns True for a first delivery. Redeliveries keep the earliest receipt."""
        cur = self.conn.execute(
            """
            INSERT INTO events (event_id, user_id, event, received_at, occurred_at, sent_at, session_id,
                                app_dialect, app_language, platform, properties, source)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(event_id) DO UPDATE SET
                deliveries = deliveries + 1,
                received_at = MIN(received_at, excluded.received_at)
            RETURNING deliveries
            """,
            self._event_tuple(e, source),
        )
        return cur.fetchone()[0] == 1

    def ingest_events_bulk(self, events: Iterable[dict[str, Any]], source: str) -> tuple[int, int]:
        """Bulk version for history load. Returns (lines, redeliveries)."""
        lines = 0
        self.conn.execute("BEGIN")
        try:
            for e in events:
                lines += 1
                self.conn.execute(
                    """
                    INSERT INTO events (event_id, user_id, event, received_at, occurred_at, sent_at, session_id,
                                        app_dialect, app_language, platform, properties, source)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(event_id) DO UPDATE SET
                        deliveries = deliveries + 1,
                        received_at = MIN(received_at, excluded.received_at)
                    """,
                    self._event_tuple(e, source),
                )
            self.conn.execute("COMMIT")
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise
        redelivered = self.conn.execute(
            "SELECT COALESCE(SUM(deliveries - 1), 0) FROM events WHERE source = ?", (source,)
        ).fetchone()[0]
        return lines, int(redelivered)

    @staticmethod
    def _event_tuple(e: dict[str, Any], source: str) -> tuple:
        return (
            e["event_id"], e["user_id"], e["event"], e["received_at"], e.get("occurred_at"), e.get("sent_at"),
            e.get("session_id"), e.get("app_dialect"), e.get("app_language"), e.get("platform"),
            json.dumps(e.get("properties") or {}, ensure_ascii=False), source,
        )

    def events_for(self, user_id: str, known_at: str, events: tuple[str, ...] | None = None) -> list[dict[str, Any]]:
        """Events for one viewer that had been received by `known_at` (the agent's knowledge cut-off)."""
        sql = "SELECT * FROM events WHERE user_id = ? AND received_at <= ?"
        args: list[Any] = [user_id, known_at]
        if events:
            sql += " AND event IN (%s)" % ",".join("?" * len(events))
            args.extend(events)
        sql += " ORDER BY received_at, event_id"
        return [_row_to_event(r) for r in self.conn.execute(sql, args)]

    def events_between(self, user_id: str, start: str, end: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM events WHERE user_id = ? AND received_at >= ? AND received_at <= ? "
            "ORDER BY received_at, event_id",
            (user_id, start, end),
        )
        return [_row_to_event(r) for r in rows]

    def get_event(self, event_id: str) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT * FROM events WHERE event_id = ?", (event_id,)).fetchone()
        return None if row is None else _row_to_event(row)

    # -- user data store: profiles / arms ------------------------------------------------------------------

    def load_profiles(self, rows: Iterable[dict[str, str]]) -> int:
        n = 0
        self.conn.execute("BEGIN")
        for r in rows:
            n += 1
            self.conn.execute(
                "INSERT INTO profile_rows (user_id, payload, updated_at, snapshot_at) VALUES (?, ?, ?, ?)",
                (r["user_id"], json.dumps(r, ensure_ascii=False), r.get("updated_at"), r.get("snapshot_at")),
            )
        self.conn.execute("COMMIT")
        return n

    def profile_rows(self, user_id: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT row_id, payload FROM profile_rows WHERE user_id = ? ORDER BY row_id", (user_id,)
        )
        return [dict(json.loads(r["payload"]), _row_id=r["row_id"]) for r in rows]

    def load_assignments(self, rows: Iterable[tuple[str, str]]) -> int:
        self.conn.execute("BEGIN")
        n = 0
        for user_id, arm in rows:
            n += 1
            self.conn.execute(
                "INSERT INTO assignments (user_id, arm) VALUES (?, ?) ON CONFLICT(user_id) DO UPDATE SET arm = excluded.arm",
                (user_id, arm),
            )
        self.conn.execute("COMMIT")
        return n

    def arm(self, user_id: str) -> str | None:
        row = self.conn.execute("SELECT arm FROM assignments WHERE user_id = ?", (user_id,)).fetchone()
        return None if row is None else row["arm"]

    # -- trigger idempotency --------------------------------------------------------------------------------

    def claim_trigger(self, event_id: str, user_id: str, deliver_at: str) -> tuple[bool, str | None]:
        """(first_delivery, existing_decision_id)."""
        row = self.conn.execute(
            "SELECT decision_id FROM processed_triggers WHERE event_id = ?", (event_id,)
        ).fetchone()
        if row is not None:
            self.conn.execute(
                "UPDATE processed_triggers SET deliveries = deliveries + 1, last_deliver_at = ? WHERE event_id = ?",
                (deliver_at, event_id),
            )
            return False, row["decision_id"]
        self.conn.execute(
            "INSERT INTO processed_triggers (event_id, user_id, first_deliver_at, last_deliver_at) VALUES (?, ?, ?, ?)",
            (event_id, user_id, deliver_at, deliver_at),
        )
        return True, None

    # -- decisions, facts, actions --------------------------------------------------------------------------

    def save_decision(self, conn: sqlite3.Connection, d: dict[str, Any]) -> None:
        conn.execute(
            """
            INSERT INTO decisions (decision_id, viewer_id, trigger_event_id, deliver_at, published_at, status,
                                   action, goal_id, payload)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                d["decision_id"], d["viewer_id"], d["trigger"]["event_id"], d["trigger"]["deliver_at"],
                d.get("published_at"), d["status"], d["action"], (d.get("goal") or {}).get("id"),
                json.dumps(d, ensure_ascii=False),
            ),
        )
        conn.execute(
            "UPDATE processed_triggers SET decision_id = ? WHERE event_id = ?",
            (d["decision_id"], d["trigger"]["event_id"]),
        )

    def save_facts(self, conn: sqlite3.Connection, viewer_id: str, decision_id: str, known_at: str,
                   facts: list[dict[str, Any]]) -> None:
        conn.executemany(
            """
            INSERT INTO facts (viewer_id, decision_id, fact_key, value, source, source_id, observed_at, known_at, freshness_s)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (viewer_id, decision_id, f["key"], json.dumps(f["value"], ensure_ascii=False), f["source"],
                 f.get("source_id"), f.get("observed_at"), known_at, f.get("freshness_s"))
                for f in facts
            ],
        )

    def get_decision(self, decision_id: str) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT payload FROM decisions WHERE decision_id = ?", (decision_id,)).fetchone()
        return None if row is None else json.loads(row["payload"])

    def decisions_for(self, viewer_id: str, until: str | None = None) -> list[dict[str, Any]]:
        sql, args = "SELECT payload FROM decisions WHERE viewer_id = ?", [viewer_id]
        if until:
            sql += " AND deliver_at <= ?"
            args.append(until)
        return [json.loads(r["payload"]) for r in self.conn.execute(sql + " ORDER BY deliver_at", args)]

    def all_decisions(self) -> list[dict[str, Any]]:
        return [json.loads(r["payload"]) for r in self.conn.execute("SELECT payload FROM decisions ORDER BY rowid")]

    def save_action(self, conn: sqlite3.Connection, a: dict[str, Any], message_day: str | None) -> None:
        conn.execute(
            """
            INSERT INTO actions (action_id, decision_id, viewer_id, type, published_at, send_at, message_day,
                                 dialect, title_ids, payload)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                a["action_id"], a["decision_id"], a["user_id"], a["type"], a["published_at"], a.get("send_at"),
                message_day, a.get("dialect"), json.dumps(action_title_ids(a)), json.dumps(a, ensure_ascii=False),
            ),
        )

    def actions_for(self, viewer_id: str, until: str | None = None) -> list[dict[str, Any]]:
        sql, args = "SELECT payload, superseded_by FROM actions WHERE viewer_id = ?", [viewer_id]
        if until:
            sql += " AND published_at <= ?"
            args.append(until)
        out = []
        for r in self.conn.execute(sql + " ORDER BY published_at", args):
            a = json.loads(r["payload"])
            a["superseded_by"] = r["superseded_by"]
            out.append(a)
        return out

    def all_actions(self) -> list[dict[str, Any]]:
        return [json.loads(r["payload"]) for r in self.conn.execute("SELECT payload FROM actions ORDER BY published_at, action_id")]

    def message_days(self, viewer_id: str) -> set[str]:
        rows = self.conn.execute(
            "SELECT message_day FROM actions WHERE viewer_id = ? AND message_day IS NOT NULL", (viewer_id,)
        )
        return {r["message_day"] for r in rows}

    def live_pages_with_title(self, title_id: str, since: str, until: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            """
            SELECT payload FROM actions
            WHERE type = 'home_page' AND superseded_by IS NULL AND published_at >= ? AND published_at <= ?
              AND EXISTS (SELECT 1 FROM json_each(actions.title_ids) WHERE value = ?)
            """,
            (since, until, title_id),
        )
        return [json.loads(r["payload"]) for r in rows]

    def mark_superseded(self, conn: sqlite3.Connection, action_id: str, by: str) -> None:
        conn.execute("UPDATE actions SET superseded_by = ? WHERE action_id = ?", (by, action_id))

    # -- plans ----------------------------------------------------------------------------------------------

    def open_plan(self, viewer_id: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM plans WHERE viewer_id = ? AND status = 'open'", (viewer_id,)
        ).fetchone()
        return None if row is None else _plan(row)

    def plans_for(self, viewer_id: str) -> list[dict[str, Any]]:
        return [_plan(r) for r in self.conn.execute("SELECT * FROM plans WHERE viewer_id = ? ORDER BY plan_id", (viewer_id,))]

    def all_plans(self) -> list[dict[str, Any]]:
        return [_plan(r) for r in self.conn.execute("SELECT * FROM plans ORDER BY plan_id")]

    def open_plans_for_goal(self, goal_id: str) -> list[dict[str, Any]]:
        return [_plan(r) for r in self.conn.execute(
            "SELECT * FROM plans WHERE goal_id = ? AND status = 'open'", (goal_id,))]

    def create_plan(self, conn: sqlite3.Connection, p: dict[str, Any]) -> int:
        cur = conn.execute(
            """
            INSERT INTO plans (viewer_id, arm, goal_id, rule_version, success_event, success_condition, window_hours,
                               params, started_at, deadline, decision_id, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open')
            """,
            (
                p["viewer_id"], p["arm"], p["goal_id"], p["rule_version"], p["success_event"], p["success_condition"],
                p["window_hours"], json.dumps(p.get("params") or {}), p["started_at"], p["deadline"], p["decision_id"],
            ),
        )
        plan_id = int(cur.lastrowid)
        self.plan_change(conn, plan_id, p["viewer_id"], p["started_at"], "opened", {"goal_id": p["goal_id"], "decision_id": p["decision_id"]})
        return plan_id

    def close_plan(self, conn: sqlite3.Connection, plan_id: int, status: str, at: str, reason: str,
                   signal_event_id: str | None = None) -> None:
        conn.execute(
            "UPDATE plans SET status = ?, closed_at = ?, close_reason = ?, signal_event_id = ? WHERE plan_id = ? AND status = 'open'",
            (status, at, reason, signal_event_id, plan_id),
        )
        viewer = conn.execute("SELECT viewer_id FROM plans WHERE plan_id = ?", (plan_id,)).fetchone()["viewer_id"]
        self.plan_change(conn, plan_id, viewer, at, status, {"reason": reason, "signal_event_id": signal_event_id})

    def update_plan_rule(self, conn: sqlite3.Connection, plan_id: int, version: str, success_event: str,
                         success_condition: str, at: str, detail: dict[str, Any]) -> None:
        conn.execute(
            "UPDATE plans SET rule_version = ?, success_event = ?, success_condition = ? WHERE plan_id = ?",
            (version, success_event, success_condition, plan_id),
        )
        viewer = conn.execute("SELECT viewer_id FROM plans WHERE plan_id = ?", (plan_id,)).fetchone()["viewer_id"]
        self.plan_change(conn, plan_id, viewer, at, "rule_corrected", detail)

    def plan_change(self, conn: sqlite3.Connection, plan_id: int, viewer_id: str, at: str, change: str,
                    detail: dict[str, Any]) -> None:
        conn.execute(
            "INSERT INTO plan_history (plan_id, viewer_id, at, change, detail) VALUES (?, ?, ?, ?, ?)",
            (plan_id, viewer_id, at, change, json.dumps(detail, ensure_ascii=False)),
        )

    def plan_history(self, viewer_id: str) -> list[dict[str, Any]]:
        rows = self.conn.execute("SELECT * FROM plan_history WHERE viewer_id = ? ORDER BY id", (viewer_id,))
        return [dict(dict(r), detail=json.loads(r["detail"])) for r in rows]

    # -- rules, outcomes, lessons, control ------------------------------------------------------------------

    def register_rule(self, goal_id: str, version: str, success: dict[str, Any], effective_at: str, source: str) -> None:
        self.conn.execute(
            "INSERT OR IGNORE INTO rule_versions (goal_id, version, success, effective_at, source) VALUES (?, ?, ?, ?, ?)",
            (goal_id, version, json.dumps(success, ensure_ascii=False), effective_at, source),
        )

    def rule_versions(self) -> list[dict[str, Any]]:
        rows = self.conn.execute("SELECT * FROM rule_versions ORDER BY goal_id, effective_at")
        return [dict(dict(r), success=json.loads(r["success"])) for r in rows]

    def save_outcome(self, o: dict[str, Any]) -> None:
        self.conn.execute(
            """
            INSERT INTO outcomes (plan_id, viewer_id, arm, goal_id, rule_version, status, reason, signal_event_id,
                                  signal_at, judged_at, judged_by, exposure)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(plan_id) DO UPDATE SET status = excluded.status, reason = excluded.reason,
                signal_event_id = excluded.signal_event_id, signal_at = excluded.signal_at,
                judged_at = excluded.judged_at, judged_by = excluded.judged_by, exposure = excluded.exposure
            """,
            (
                o["plan_id"], o["viewer_id"], o["arm"], o["goal_id"], o["rule_version"], o["status"], o["reason"],
                o.get("signal_event_id"), o.get("signal_at"), o["judged_at"], o["judged_by"],
                json.dumps(o.get("exposure") or {}),
            ),
        )

    def outcomes(self, viewer_id: str | None = None) -> list[dict[str, Any]]:
        sql, args = "SELECT * FROM outcomes", []
        if viewer_id:
            sql, args = sql + " WHERE viewer_id = ?", [viewer_id]
        return [dict(dict(r), exposure=json.loads(r["exposure"])) for r in self.conn.execute(sql + " ORDER BY plan_id", args)]

    def add_lesson(self, viewer_id: str, at: str, kind: str, detail: str, evidence: list[str]) -> None:
        self.conn.execute(
            "INSERT OR IGNORE INTO lessons (viewer_id, at, kind, detail, evidence) VALUES (?, ?, ?, ?, ?)",
            (viewer_id, at, kind, detail, json.dumps(evidence)),
        )

    def lessons(self, viewer_id: str, until: str | None = None) -> list[dict[str, Any]]:
        sql, args = "SELECT * FROM lessons WHERE viewer_id = ?", [viewer_id]
        if until:
            sql += " AND at <= ?"
            args.append(until)
        return [dict(dict(r), evidence=json.loads(r["evidence"])) for r in self.conn.execute(sql + " ORDER BY id", args)]

    def log_control(self, at: str, type_: str, payload: dict[str, Any], affected: dict[str, Any]) -> None:
        self.conn.execute(
            "INSERT INTO control_log (at, type, payload, affected) VALUES (?, ?, ?, ?)",
            (at, type_, json.dumps(payload, ensure_ascii=False), json.dumps(affected, ensure_ascii=False)),
        )

    def control_log(self) -> list[dict[str, Any]]:
        rows = self.conn.execute("SELECT * FROM control_log ORDER BY id")
        return [dict(dict(r), payload=json.loads(r["payload"]), affected=json.loads(r["affected"])) for r in rows]

    def trigger_deliveries(self) -> list[dict[str, Any]]:
        return [dict(r) for r in self.conn.execute("SELECT * FROM processed_triggers")]


def _plan(row: sqlite3.Row) -> dict[str, Any]:
    p = dict(row)
    p["params"] = json.loads(p["params"])
    return p


def action_title_ids(a: dict[str, Any]) -> list[str]:
    ids: list[str] = []
    if a.get("hero_title_id"):
        ids.append(a["hero_title_id"])
    for row in a.get("rows") or []:
        ids.extend(row.get("title_ids") or [])
    if a.get("title_id"):
        ids.append(a["title_id"])
    return ids
