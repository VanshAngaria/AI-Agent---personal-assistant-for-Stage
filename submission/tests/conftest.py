from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from oua.config import Settings
from oua.goals import GoalCatalog
from oua.schemas import (
    ActionType,
    Checks,
    Decision,
    DecisionStatus,
    DialectInfo,
    EvidenceDetail,
    ExecutionAck,
    GoalRef,
    SuccessSignal,
    TriggerRef,
)
from oua.service import DecisionService
from oua.state.store import Store

UTC = timezone.utc
TEST_VIEWER = "TEST-VIEWER-001"
CATALOG_VERSION = "2026-09-30"


@pytest.fixture
def catalog() -> GoalCatalog:
    path = Path(__file__).resolve().parents[1] / "config" / "goals.json"
    return GoalCatalog.load(path)


@pytest.fixture
def store(tmp_path: Path) -> Store:
    db = Store(tmp_path / "agent.sqlite")
    yield db
    db.close()


@pytest.fixture
def service(store: Store, catalog: GoalCatalog) -> DecisionService:
    settings = Settings(db_path=store.db_path, pack_path=None)
    return DecisionService(store=store, catalog=catalog, settings=settings)


@pytest.fixture
def fixture_dir() -> Path:
    return Path(__file__).resolve().parent / "fixtures"


def utc(stamp: str) -> datetime:
    return datetime.fromisoformat(stamp.replace("Z", "+00:00")).astimezone(UTC)


def g01_goal() -> GoalRef:
    return GoalRef(
        id="G01",
        deadline=utc("2026-10-02T12:00:00.000Z"),
        success_signal=SuccessSignal(
            event="play_started",
            condition="any title",
            window_hours=24,
        ),
        rule_version=CATALOG_VERSION,
    )


def base_kwargs(**overrides: object) -> dict:
    data: dict = {
        "decision_id": "D-TEST-1",
        "viewer_id": TEST_VIEWER,
        "trigger": TriggerRef(
            event="app_open",
            event_id="evt-test-1",
            received_at=utc("2026-10-01T12:00:00.000Z"),
            deliver_at=utc("2026-10-01T12:00:00.000Z"),
            event_age_s=2.0,
        ),
        "goal": g01_goal(),
        "action": ActionType.HOME_PAGE,
        "dialect": DialectInfo(used="d1", source="app_setting", conflicts=[]),
        "content": ["T-TEST-1", "T-TEST-2", "T-TEST-3"],
        "confidence": 0.4,
        "confidence_reason": "Heuristic: 1 conflict, profile missing; not calibrated.",
        "evidence": ["fact:signup"],
        "evidence_details": [
            EvidenceDetail(
                id="fact:signup",
                source="test_fixture",
                timestamp=utc("2026-10-01T11:50:00.000Z"),
                freshness_s=600.0,
                note="Synthetic fixture for schema tests; not pack evidence.",
            )
        ],
        "assumptions": ["Fixture assumes no pack event stream is attached yet."],
        "conflicting_evidence": [],
        "checks": Checks(passed=["eligibility"], failed=[]),
        "decided_in_ms": 12,
        "decision_deadline_ms": 60_000,
        "status": DecisionStatus.PUBLISHED,
        "execution": ExecutionAck(
            confirmed=True,
            confirmed_at=utc("2026-10-01T12:00:01.000Z"),
            executor="test_harness",
        ),
    }
    data.update(overrides)
    return data


def make_decision(**overrides: object) -> Decision:
    return Decision.model_validate(base_kwargs(**overrides))
