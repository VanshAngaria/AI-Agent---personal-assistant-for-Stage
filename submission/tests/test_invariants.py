from __future__ import annotations

from oua.config import Settings
from tests.conftest import TEST_VIEWER


def test_settings_pack_path_optional():
    settings = Settings(pack_path=None)
    assert settings.pack_path is None
    assert settings.decision_deadline_ms == 60_000
    assert settings.max_model_calls == 8
    assert settings.max_tool_calls == 20


def test_facts_keep_provenance(store):
    store.add_fact(
        TEST_VIEWER,
        "preferred_dialect",
        {"value": "d1"},
        source="profile_snapshot",
        source_id="row-a",
        at="2026-09-29T11:00:00.000Z",
    )
    store.add_fact(
        TEST_VIEWER,
        "preferred_dialect",
        {"value": "d2"},
        source="profile_snapshot",
        source_id="row-b",
        at="2026-09-22T02:00:00.000Z",
    )
    facts = store.list_facts(TEST_VIEWER)
    assert len(facts) == 2
    assert {item["source_id"] for item in facts} == {"row-a", "row-b"}
    assert all(item["source"] == "profile_snapshot" for item in facts)
