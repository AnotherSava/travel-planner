"""Pipeline behaviour, mostly regressions from real data in Detroit and Vancouver."""

from __future__ import annotations

import pytest

from travel_planner.model import Address, City, Ranking
from travel_planner.pipeline import (
    _latin_label,
    clean_description,
    clean_name,
    haversine_km,
    is_latin,
    place_aliases,
)
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


# --- redundant geography in Wikidata descriptions --------------------------------

VANCOUVER = frozenset({"vancouver", "british columbia", "canada"})
DETROIT = frozenset(
    {
        "detroit",
        "wayne county",
        "michigan",
        "united states",
        "united states of america",
        "usa",
        "u.s.",
        "us",
    }
)


@pytest.mark.parametrize(
    ("aliases", "description", "expected"),
    [
        # Wikidata disambiguates against the whole world, so it names the place every
        # time. In a one-city list that suffix is on every row and distinguishes nothing.
        (VANCOUVER, "stadium at Vancouver, British Columbia, Canada", "stadium"),
        (VANCOUVER, "peninsula in British Columbia, Canada", "peninsula"),
        (VANCOUVER, "airport located in British Columbia, Canada", "airport"),
        (VANCOUVER, "park in Vancouver, British Columbia, Canada", "park"),
        (DETROIT, "indoor arena in Detroit, Michigan, USA", "indoor arena"),
        (
            DETROIT,
            "group of interconnected skyscrapers in Detroit, Michigan, "
            "United States of America",
            "group of interconnected skyscrapers",
        ),
        # "of Anthropology" is the description; only the trailing "in <place>" goes.
        (
            VANCOUVER,
            "museum of Anthropology in Vancouver, British Columbia, Canada",
            "museum of Anthropology",
        ),
        # A multi-word connective must not leave "located" dangling.
        (
            DETROIT,
            "American public research university located in Detroit, Michigan",
            "American public research university",
        ),
        # A qualifier binds the place name and says something the bare name does not,
        # so it survives — stripping the alias alone left a dangling "Greater".
        (
            VANCOUVER,
            "public research university in Greater Vancouver, British Columbia, Canada",
            "public research university in Greater Vancouver",
        ),
        # Another place entirely: only the country is redundant.
        (VANCOUVER, "1989 mass shooting in Montréal, Canada", "1989 mass shooting in Montréal"),
        # The location is mid-string with meaning after it — nothing may be removed.
        (
            VANCOUVER,
            "indoor arena in Vancouver, Canada, mainly used for ice hockey",
            "indoor arena in Vancouver, Canada, mainly used for ice hockey",
        ),
        # Here the geography *is* the description.
        (
            DETROIT,
            "privately owned suspension bridge that connects Detroit, Michigan, "
            "with Windsor, Ontario",
            "privately owned suspension bridge that connects Detroit, Michigan, "
            "with Windsor, Ontario",
        ),
        # A different country is never this city's redundant suffix.
        (
            DETROIT,
            "university in Windsor, Ontario, Canada",
            "university in Windsor, Ontario, Canada",
        ),
        (DETROIT, "home venue of the Detroit Tigers", "home venue of the Detroit Tigers"),
        (DETROIT, "Formula 1 Grand Prix", "Formula 1 Grand Prix"),
        # A bulk-import artifact on nine Detroit parks, which also blocked the
        # geography tail behind it from being recognised.
        (
            DETROIT,
            "park in Wayne County, Michigan, United States of America; geonames ID = 4993084",
            "park",
        ),
        # A semicolon clause is usually real content and must survive.
        (
            VANCOUVER,
            "park in Vancouver, British Columbia, Canada; adjacent to Dr. Sun Yat-Sen Garden",
            "park in Vancouver, British Columbia, Canada; adjacent to Dr. Sun Yat-Sen Garden",
        ),
        # Never reduce a description to nothing.
        (DETROIT, "Detroit", "Detroit"),
        (VANCOUVER, "", ""),
    ],
)
def test_clean_description(aliases, description, expected):
    assert clean_description(description, aliases) == expected


def test_place_aliases_expands_country_variants():
    """Wikidata writes the same country several ways: USA, United States of America."""
    city = City(
        slug="detroit-michigan",
        display_name="Detroit",
        osm_type="relation",
        osm_id=134591,
        address_type="city",
        centre_lat=42.33,
        centre_lon=-83.05,
        address=Address(city="Detroit", state="Michigan", country="United States"),
    )
    aliases = place_aliases(city)
    assert {"detroit", "michigan", "united states", "united states of america", "usa"} <= aliases


def test_county_counts_as_this_place(city):
    """Nine Detroit park descriptions name only "Wayne County, Michigan"."""
    assert "wayne county" in place_aliases(
        City(
            slug="detroit-michigan",
            display_name="Detroit",
            osm_type="relation",
            osm_id=134591,
            address_type="city",
            centre_lat=42.33,
            centre_lon=-83.05,
            address=Address(city="Detroit", county="Wayne County", state="Michigan"),
        )
    )
