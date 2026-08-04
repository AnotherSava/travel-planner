"""Wikipedia extract sourcing — mostly the title join, which fails silently."""

from __future__ import annotations

import pytest

from travel_planner.sources.wikipedia import Article, english_title, resolve_titles


def test_english_title_takes_only_the_english_prefix():
    """The OSM tag is `lang:Title` with mixed prefixes — a Lisbon pull carried 183 pt,
    46 en, 4 de and 1 fr. Only the English ones are usable directly."""
    assert english_title("en:Stanley Park") == "Stanley Park"
    assert english_title("pt:Torre de Belém") is None
    assert english_title("Stanley Park") is None
    assert english_title(None) is None


def test_resolve_titles_follows_normalisation_then_redirects():
    """A redirect's `from` is the *normalised* title, not the raw one, so the two
    tables have to be applied in that order."""
    query = {
        "normalized": [{"from": "stanley_park", "to": "Stanley park"}],
        "redirects": [{"from": "Stanley park", "to": "Stanley Park"}],
    }
    assert resolve_titles(["stanley_park"], query)["stanley_park"] == "Stanley Park"


def test_resolve_titles_collapses_several_inputs_onto_one_article():
    """Twenty titles routinely come back as fewer pages. Positional matching then
    attaches the wrong prose to the wrong pin, and does it silently."""
    query = {"redirects": [{"from": "Hotel Vancouver", "to": "Fairmont Hotel Vancouver"}]}
    resolved = resolve_titles(["Hotel Vancouver", "Fairmont Hotel Vancouver"], query)
    assert resolved == {
        "Hotel Vancouver": "Fairmont Hotel Vancouver",
        "Fairmont Hotel Vancouver": "Fairmont Hotel Vancouver",
    }


def test_resolve_titles_leaves_untouched_titles_alone():
    assert resolve_titles(["Sun Tower"], {}) == {"Sun Tower": "Sun Tower"}


@pytest.mark.parametrize(
    ("title", "extract", "expected"),
    [
        # Wikipedia's house style opens "<Title> is a <definition>", restating the name
        # the reader just read — the same tautology the Wikidata descriptions had.
        (
            "St. Andrew's-Wesley United Church",
            "St. Andrew's-Wesley United Church is an affirming church in the downtown core.",
            "An affirming church in the downtown core.",
        ),
        (
            "Book Tower",
            "Book Tower is a 38-story mixed-use building.",
            "A 38-story mixed-use building.",
        ),
        # A leading article belongs to the sentence, not the title.
        (
            "Marine Building",
            "The Marine Building is a skyscraper on Burrard Street.",
            "A skyscraper on Burrard Street.",
        ),
        # Anything not matching the pattern exactly is left alone rather than mangled.
        (
            "Michigan Central Station",
            "Michigan Central Station (MCS, also known as Michigan Central Depot) is the depot.",
            "Michigan Central Station (MCS, also known as Michigan Central Depot) is the depot.",
        ),
    ],
)
def test_summary_drops_the_definitional_opener(title, extract, expected):
    assert Article(key="way/1", title=title, extract=extract).summary == expected


def test_summary_does_not_split_on_an_initial():
    extract = "St. Joseph Shrine is a church founded by Fr. J. Smith in 1873. It has spires."
    assert Article(key="way/1", title="St. Joseph Shrine", extract=extract).summary == (
        "A church founded by Fr. J. Smith in 1873."
    )
