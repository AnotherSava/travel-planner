"""Selection semantics — specifically, that selecting is not inverse rejection."""

from __future__ import annotations

import pytest

from travel_planner.whitelist import Whitelist


@pytest.fixture(scope="module")
def whitelist() -> Whitelist:
    return Whitelist.load()


def test_selects_a_plain_museum(whitelist):
    assert whitelist.accepts({"name": "Detroit Institute of Arts", "tourism": "museum"})


def test_requires_a_name(whitelist):
    assert not whitelist.accepts({"tourism": "museum"})


def test_excluded_value_still_enters_via_another_predicate(whitelist):
    """The regression this whole design exists for.

    Teatro Nacional Dona Maria II carries historic=yes, which is never selected. It
    arrives through amenity=theatre and must survive — a reject-if-present filter
    would delete it, and the recall oracle only counts what is missing after you
    already believe the filter is right.
    """
    tags = {
        "name": "Teatro Nacional Dona Maria II",
        "amenity": "theatre",
        "historic": "yes",
    }
    assert whitelist.accepts(tags)
    assert whitelist.category(whitelist.matched_categories(tags)) == "theatre"


def test_private_palacete_is_not_a_castle(whitelist):
    """185 of 1,032 Lisbon elements were historic=castle; almost none were castles."""
    assert not whitelist.accepts(
        {
            "name": "Palacete Bessone",
            "historic": "castle",
            "castle_type": "palace",
            "building": "house",
        }
    )


def test_real_castle_survives_the_castle_type_guard(whitelist):
    assert whitelist.accepts({"name": "Castelo de São Jorge", "historic": "castle"})


def test_notable_street_tree_is_not_an_attraction(whitelist):
    """tourism=attraction in Lisbon included 11 individual street trees."""
    assert not whitelist.accepts(
        {"name": "Bela-sombra do Príncipe Real", "tourism": "attraction", "natural": "tree"}
    )


def test_gated_category_needs_a_link(whitelist):
    parish = {"name": "Igreja de São Nicolau", "amenity": "place_of_worship"}
    assert not whitelist.accepts(parish)
    assert whitelist.accepts({**parish, "wikidata": "Q123"})


def test_category_precedence_is_deterministic(whitelist):
    """Torre de Belém is both tourism=attraction and historic=fort."""
    tags = {"name": "Torre de Belém", "tourism": "attraction", "historic": "fort"}
    matched = whitelist.matched_categories(tags)
    assert set(matched) == {"attraction", "fortification"}
    assert whitelist.category(matched) == "fortification"


def test_geom_and_center_stay_in_separate_out_statements(whitelist):
    """`out center geom` is a trap: the later mode wins and every centre is lost.

    Verified live — that form returned geometry for 6 ways and zero centres, leaving
    relations with no coordinate at all. The two modes must be separate statements.
    """
    query = whitelist.overpass_query((42.33, -83.05), 25000)
    assert ".result out tags geom;" in query
    assert ".result out tags bb;" in query
    assert "out center geom" not in query
    assert "out geom center" not in query


def test_query_is_bounded_by_radius_not_municipality(whitelist):
    """Capilano and The Henry Ford are outside their city's boundary but not its reach."""
    query = whitelist.overpass_query((49.2609, -123.1139), 25000)
    assert "area(" not in query
    # A bbox, not `around:` — the radial form 504s on the public instance at this size.
    assert "around:" not in query
    assert "(49.03" in query and "(-123" not in query.split("(49.03")[0][-3:]
