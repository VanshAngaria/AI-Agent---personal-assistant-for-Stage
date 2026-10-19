from __future__ import annotations

import pytest
from pydantic import ValidationError

from oua.schemas import ConflictRecord, EvidenceDetail
from tests.conftest import make_decision, utc

T0 = utc("2026-10-01T12:00:00.000Z")


def test_cited_evidence_must_have_matching_details():
    with pytest.raises(ValidationError, match="matching evidence_details"):
        make_decision(evidence=["fact:signup", "event:missing"])


def test_detail_without_citation_rejected():
    details = [
        EvidenceDetail(
            id="fact:signup",
            source="test_fixture",
            timestamp=T0,
            freshness_s=0,
            note="Constructed for schema tests; not pack evidence.",
        ),
        EvidenceDetail(
            id="orphan",
            source="test_fixture",
            timestamp=T0,
            freshness_s=0,
        ),
    ]
    with pytest.raises(ValidationError, match="matching evidence_details"):
        make_decision(evidence=["fact:signup"], evidence_details=details)


def test_duplicate_detail_ids_rejected():
    details = [
        EvidenceDetail(id="fact:signup", source="test_fixture", timestamp=T0, freshness_s=0),
        EvidenceDetail(id="fact:signup", source="other", timestamp=T0, freshness_s=1),
    ]
    with pytest.raises(ValidationError, match="unique"):
        make_decision(evidence=["fact:signup"], evidence_details=details)


def test_conflict_requires_two_sources_and_explanation():
    decision = make_decision(
        conflicting_evidence=[
            ConflictRecord(
                fact="dialect",
                sources=["profile:d1", "app_setting:d2"],
                chosen="app_setting:d2",
                why="Homepage is requested for the app dialect currently set.",
            )
        ]
    )
    assert decision.conflicting_evidence[0].chosen == "app_setting:d2"


def test_conflict_with_one_source_rejected():
    with pytest.raises(ValidationError, match="at least 2"):
        make_decision(
            conflicting_evidence=[
                ConflictRecord(
                    fact="dialect",
                    sources=["profile:d1"],
                    chosen="profile:d1",
                    why="only one source listed",
                )
            ]
        )


def test_evidence_detail_carries_source_and_freshness():
    decision = make_decision()
    detail = decision.evidence_details[0]
    assert detail.source == "test_fixture"
    assert detail.timestamp is not None
    assert detail.freshness_s == 600.0
    assert "not pack evidence" in (detail.note or "")
