"""Pipeline behaviour, mostly regressions from real data in Detroit and Vancouver."""

from __future__ import annotations

import pytest

from travel_planner.model import Address, City, Ranking
from travel_planner.pipeline import _latin_label, clean_name, haversine_km, is_latin
from travel_planner.sources.wikidata import Entity


@pytest.fixture
def city() -> City:
    return City(
        slug="vancouver-british-columbia",
        display_name="Vancouver",
        osm_type="relation",
        osm_id=1852574,
        address_type="city",
        centre_lat=49.2609,
        centre_lon=-123.1139,
        address=Address(),
    )


def test_zero_width_space_is_stripped():
    """A Lisbon parish name begins with U+200B, which breaks name dedup silently."""
    assert clean_name("​Paroisse Saint-Sacrement") == "Paroisse Saint-Sacrement"


def test_latin_detection():
    assert is_latin("Marker of Change")
    assert is_latin("Théâtre du Châtelet")
    assert not is_latin("新熊野神社")


def test_latin_name_beats_the_wikidata_label():
    """The regression that renamed a memorial after the event it commemorates.

    Vancouver's "Marker of Change" carries a QID describing the 1989 École
    Polytechnique massacre. The QID has a coordinate, so the P625 gate does not catch
    it — only preferring an already-Latin local name does.
    """
    entity = Entity(qid="Q1234", sitelinks=26, label="École Polytechnique massacre")
    label, source = _latin_label({"name": "Marker of Change"}, entity)
    assert label == "Marker of Change"
    assert source.value == "local"


def test_wikidata_label_rescues_a_non_latin_name():
    entity = Entity(qid="Q5678", sitelinks=12, label="Imakumano Shrine")
    label, source = _latin_label({"name": "新熊野神社"}, entity)
    assert label == "Imakumano Shrine"
    assert source.value == "wikidata_label"


def test_name_en_wins_over_everything():
    entity = Entity(qid="Q1", sitelinks=3, label="Wrong Label")
    label, source = _latin_label({"name": "織成館", "name:en": "Orinasu-kan"}, entity)
    assert label == "Orinasu-kan"
    assert source.value == "name:en"


def test_haversine_matches_a_known_distance():
    # Vancouver downtown to Stanley Park, roughly 5 km.
    assert 4.0 < haversine_km(49.2609, -123.1139, 49.3017, -123.1417) < 6.0


def test_ranking_enum_defaults_to_unranked():
    assert Ranking.UNRANKED.value == "unranked"
