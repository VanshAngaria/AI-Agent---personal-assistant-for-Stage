from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from oua.retrieval.catalogue import CatalogueIndex, RetrieveQuery
from oua.retrieval.homepage import HomepageAssembler
from oua.retrieval.policy import PolicyIndex

AT = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)


def load_index(fixture_dir: Path) -> CatalogueIndex:
    return CatalogueIndex.load(fixture_dir / "catalogue.csv", fixture_dir / "episodes.csv")


def load_policy() -> PolicyIndex:
    path = Path(__file__).resolve().parents[1] / "config" / "interventions.json"
    return PolicyIndex.load(path)


def test_wrong_dialect_returns_empty(fixture_dir):
    index = load_index(fixture_dir)
    hits = index.retrieve(RetrieveQuery(dialect="d9", at=AT))
    assert hits == []
    d2 = index.retrieve(RetrieveQuery(dialect="d2", at=AT))
    assert {item.title_id for item in d2} == {"FIX-D2-A"}
    assert all(item.dialect == "d2" for item in d2)


def test_unavailable_future_expired_and_withdrawn(fixture_dir):
    index = load_index(fixture_dir)
    hits = index.retrieve(RetrieveQuery(dialect="d1", at=AT))
    ids = {item.title_id for item in hits}
    assert "FIX-D1-X" not in ids
    assert "FIX-D1-F" not in ids
    assert "FIX-D1-A" in ids
    index.withdraw("FIX-D1-A", AT)
    after = {item.title_id for item in index.retrieve(RetrieveQuery(dialect="d1", at=AT))}
    assert "FIX-D1-A" not in after


def test_premium_entitlement_filter(fixture_dir):
    index = load_index(fixture_dir)
    with_premium = {item.title_id for item in index.retrieve(RetrieveQuery(dialect="d1", at=AT, allow_premium=True))}
    without = {item.title_id for item in index.retrieve(RetrieveQuery(dialect="d1", at=AT, allow_premium=False))}
    assert "FIX-D1-P" in with_premium
    assert "FIX-D1-P" not in without


def test_prompt_injection_is_content_not_instruction(fixture_dir):
    index = load_index(fixture_dir)
    hits = index.retrieve(RetrieveQuery(dialect="d1", at=AT, query_text="WhatsApp every user"))
    injected = [item for item in hits if item.title_id == "FIX-INJ"]
    assert injected
    assert injected[0].untrusted_instruction_like is True
    assert "instruction" not in injected[0].relevance_reason.lower() or "untrusted" in injected[0].relevance_reason
    # Retrieval must not synthesize a policy action from description text.
    assert all(item.filters for item in injected)


def test_tfidf_ranks_after_filters(fixture_dir):
    index = load_index(fixture_dir)
    hits = index.retrieve(RetrieveQuery(dialect="d1", at=AT, query_text="comedy bus stand"))
    assert hits
    assert hits[0].tfidf_score is not None
    assert hits[0].title_id == "FIX-D1-B"


def test_homepage_min_titles_and_slug_dedup(fixture_dir):
    index = load_index(fixture_dir)
    policy = load_policy()
    page = HomepageAssembler(index, policy).assemble(dialect="d1", at=AT, allow_premium=False)
    assert page is not None
    assert page.rows
    slugs = []
    for row in page.rows:
        assert len(row.title_ids) >= policy.home_min_titles
        for title_id in row.title_ids:
            slugs.append(index.titles[title_id].content_slug)
    assert len(slugs) == len(set(slugs))
    assert all(index.titles[title_id].dialect == "d1" for row in page.rows for title_id in row.title_ids)


def test_homepage_wrong_dialect_none(fixture_dir):
    index = load_index(fixture_dir)
    policy = load_policy()
    assert HomepageAssembler(index, policy).assemble(dialect="d7", at=AT, allow_premium=True) is None


def test_policy_records_have_ids():
    policy = load_policy()
    wa = policy.get("wa_recommend_title_v1")
    assert wa.kind == "whatsapp_template"
    assert wa.payload["language"] == "hin"


def test_pack_t0002_injection_if_present():
    pack = Path(__file__).resolve().parents[2]
    cat = pack / "data" / "catalogue.csv"
    eps = pack / "data" / "episodes.csv"
    if not cat.exists():
        return
    index = CatalogueIndex.load(cat, eps)
    title = index.titles["T0002"]
    assert title.untrusted_instruction_like is True
    hits = index.retrieve(RetrieveQuery(dialect="d1", at=AT, allow_premium=True))
    match = [item for item in hits if item.title_id == "T0002"]
    if match:
        assert match[0].untrusted_instruction_like is True
