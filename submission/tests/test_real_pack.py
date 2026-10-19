"""Checks against the supplied data pack. Skipped when the pack is not next to the submission."""

import json

import pytest

from oua.catalogue import Catalogue
from oua.goals import RuleBook
from oua.measure import _pilot
from oua.pack import Pack, read_jsonl
from oua.policy import Policy
from oua.settings import DEFAULT_PACK

pytestmark = pytest.mark.skipif(not (DEFAULT_PACK / "data" / "catalogue.csv").exists(), reason="data pack not present")


def test_real_catalogue_instruction_text_is_flagged():
    cat = Catalogue.load(DEFAULT_PACK / "data" / "catalogue.csv", DEFAULT_PACK / "data" / "episodes.csv")
    flagged = cat.screen(Policy.load().safety["instruction_patterns"])
    assert flagged == ["T0002"]  # exactly one row, and no false positives elsewhere


def test_real_g03_correction_inherits_window():
    rules = RuleBook(json.loads((DEFAULT_PACK / "config" / "goals.json").read_text(encoding="utf-8")))
    c = next(x for x in read_jsonl(DEFAULT_PACK / "replay" / "control_events.jsonl") if x["type"] == "goal_rule_corrected")
    rv = rules.correct_success(c["goal_id"], c["new_success"], c["at"], "control")
    assert rv.success["event"] == "play_started" and rv.window_hours == 48


def test_unmeasurable_goals_in_real_config():
    rules = RuleBook(json.loads((DEFAULT_PACK / "config" / "goals.json").read_text(encoding="utf-8")))
    assert {g for g in rules.goal_ids() if not rules.current(g).measurable} == {"G10", "G11"}


def test_pilot_headline_reproduces_and_itt_reverses_it():
    p = _pilot(Pack.open(DEFAULT_PACK))
    assert p["served"] == (87, 217) and p["everyone_else"] == (649, 3674)
    assert p["treatment_itt"] == (349, 1912) and p["holdout_itt"] == (387, 1979)
    assert p["naive"]["diff"] > 0.2 and p["itt"]["diff"] < 0
