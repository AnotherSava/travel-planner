"""Worth scoring — that fame and worth are separated, and how.

Every test here names the real POI that motivated it. The failures this module
guards against were all observed on the two test cities, and every one of them was
invisible in the ranking: each subject is a genuinely famous place that no visitor
would go to look at.
"""

from __future__ import annotations

import pytest

from travel_planner.model import Confidence, LabelSource, Poi, Ranking, WorthMode
from travel_planner.sources.wikidata import Facts
from travel_planner.sources.wikivoyage import Listing, parse_listings
from travel_planner.worth import DEFAULT_CUT, WorthTable, apply_cut, assess, join_listings

from travel_planner.geometry import CoordinateSource  # isort: skip


@pytest.fixture(scope="module")
def table() -> WorthTable:
    return WorthTable.load()


def make_poi(
    label: str,
    *,
    category: str = "landmark",
    qid: str | None = None,
    sitelinks: int | None = None,
    distance: float = 2.0,
    lat: float = 49.28,
    lon: float = -123.11,
    osm_id: int = 1,
) -> Poi:
    return Poi(
        osm_type="way",
        osm_id=osm_id,
        name=label,
        label=label,
        label_source=LabelSource.LOCAL,
        lat=lat,
        lon=lon,
        coordinate_source=CoordinateSource.POLYLABEL,
        category=category,
        matched=[category],
        wikidata=qid,
        sitelinks=sitelinks,
        ranking=Ranking.SITELINKS if sitelinks else Ranking.UNRANKED,
        distance_from_centre_km=distance,
    )


# --- the question this step exists to answer -------------------------------------


def test_stadium_is_event_not_intrinsic(table):
    """BC Place. Famous, correctly tagged, and empty unless there is a game on.

    It must not be deleted — it is a real destination on the right evening — but it
    must not read as somewhere with something to look at either.
    """
    poi = make_poi("BC Place", category="stadium", qid="Q612227", sitelinks=53)
    worth = assess(poi, table=table, facts=Facts(qid="Q612227", classes=frozenset({"stadium"})))
    assert worth.mode is WorthMode.EVENT


def test_museum_is_intrinsic(table):
    poi = make_poi("Museum of Anthropology", category="museum", qid="Q1954088", sitelinks=12)
    worth = assess(poi, table=table, facts=Facts(qid="Q1954088", classes=frozenset({"museum"})))
    assert worth.mode is WorthMode.INTRINSIC


def test_airport_is_incidental_and_falls_below_the_cut(table):
    """Vancouver International Airport ties BC Place for top sitelink rank at 53."""
    poi = make_poi("Vancouver International Airport", qid="Q503919", sitelinks=53, distance=9.0)
    worth = assess(
        poi, table=table, facts=Facts(qid="Q503919", classes=frozenset({"international airport"}))
    )
    assert worth.mode is WorthMode.INCIDENTAL
    assert worth.score < DEFAULT_CUT


# --- how the class is resolved ---------------------------------------------------


def test_wikidata_class_beats_osm_category(table):
    """New Westminster: a whole municipality carrying a monument-ish OSM category.

    It ranks third in Vancouver at 42 sitelinks. Letting the OSM category compete with
    P31 on precedence scored an entire city as something built to be looked at.
    """
    poi = make_poi("New Westminster", category="monument", qid="Q876122", sitelinks=42)
    facts = Facts(qid="Q876122", classes=frozenset({"city in british columbia"}))
    worth = assess(poi, table=table, facts=facts)
    assert worth.mode is WorthMode.INCIDENTAL
    assert worth.score < DEFAULT_CUT


def test_class_matching_is_whole_phrase_not_equality(table):
    """Wikidata says "city in British Columbia", never a tidy "city"."""
    assert table.classify(frozenset({"city in british columbia"}), set()).mode is (
        WorthMode.INCIDENTAL
    )


def test_phrase_matching_respects_word_boundaries(table):
    """`park` is intrinsic and `parking garage` is not; substring matching conflates them."""
    assert table.classify(frozenset({"parking garage"}), set()).mode is WorthMode.INCIDENTAL


def test_the_most_specific_class_wins_over_mode_precedence(table):
    """`baseball park` contains the whole word `park`, so it matches the intrinsic park
    entry as well as the event one — and intrinsic-first precedence then calls a
    ballpark a park. Real Comerica Park escaped only because Wikidata labels it
    `stadium`; any city whose data says `baseball park` would have hit this."""
    assert table.classify(frozenset({"baseball park"}), set()).mode is WorthMode.EVENT


def test_specificity_is_scoped_to_one_label_not_the_whole_entity(table):
    """A place carries several labels describing different aspects of it, and the
    wordiest is not the truest.

    Ranking whole entities by token length read Grouse Mountain as a `ski resort`
    rather than a `mountain`, cutting it from the Vancouver oracle, and the Detroit
    Institute of Arts as a `performing arts center` rather than an `art museum`.
    """
    assert table.classify(frozenset({"mountain", "ski resort"}), set()).mode is (
        WorthMode.INTRINSIC
    )
    dia = frozenset({"art museum", "museum", "performing arts center", "nonprofit organization"})
    assert table.classify(dia, set()).mode is WorthMode.INTRINSIC
    assert table.classify(frozenset({"building complex", "convention center", "skyscraper"}),
                          set()).mode is WorthMode.INTRINSIC


def test_no_generic_token_swallows_a_compound_class(table):
    """`association` is a whole word inside `association football venue`, and having it
    in the organisations list classified Swangard Stadium as an organisation."""
    assert table.classify(frozenset({"association football venue", "stadium"}), set()).mode is (
        WorthMode.EVENT
    )


def test_specificity_does_not_break_a_genuine_subphrase(table):
    """`amusement park` and `urban park` must stay intrinsic, and a water tower must not
    inherit the lighthouse-and-observation-tower prior."""
    assert table.classify(frozenset({"amusement park"}), set()).mode is WorthMode.INTRINSIC
    assert table.classify(frozenset({"urban park"}), set()).mode is WorthMode.INTRINSIC
    assert table.classify(frozenset({"water tower"}), set()).mode is WorthMode.INCIDENTAL


def test_multi_match_keeps_the_theatre(table):
    """Fox Theatre is a Detroit oracle entry and a 1928 movie palace.

    Resolving multi-matches by rejecting anything that looks like an event venue
    deletes it, which is the same error as implementing the whitelist as a reject list.
    """
    poi = make_poi("Fox Theatre", category="theatre", qid="Q1442633", sitelinks=6)
    facts = Facts(
        qid="Q1442633",
        classes=frozenset({"movie theater", "historic building"}),
        heritage=True,
        architect=True,
    )
    listing = Listing(kind="do", name="Fox Theatre", article="Detroit/Downtown", qid="Q1442633")
    worth = assess(poi, table=table, listing=listing, facts=facts)
    assert worth.mode is WorthMode.EVENT
    assert worth.score >= DEFAULT_CUT


# --- evidence rules --------------------------------------------------------------


def test_a_guide_listing_does_not_shortlist_a_plain_venue(table):
    """Swangard Stadium: a suburban ground listed in the Burnaby article as home of the
    BC High School Championships.

    A guide listing on an event venue is evidence that the *programme* is worth
    attending; it says nothing about whether the building is worth looking at. Scoring
    it as though it did shortlisted every guide-listed venue automatically — Surrey
    Arts Centre, the Shadbolt Centre and Planet Ant Theater have 0 sitelinks between
    them and all cleared the cut.
    """
    poi = make_poi("Swangard Stadium", category="stadium", qid="Q2372417", sitelinks=14,
                   distance=7.5)
    facts = Facts(qid="Q2372417", classes=frozenset({"football stadium"}))
    listing = Listing(kind="do", name="Swangard Stadium", article="Burnaby", qid="Q2372417")
    worth = assess(poi, table=table, listing=listing, facts=facts)
    assert worth.mode is WorthMode.EVENT
    assert worth.score < DEFAULT_CUT
    assert worth.confidence is Confidence.VERIFIED, "the guide still supplies the reason line"


def test_a_heritage_designation_alone_shortlists_a_venue(table):
    """The Orpheum. A listed building is worth looking at whether or not a show is on,
    and it must not depend on whether Wikidata happens to carry a style field."""
    poi = make_poi("Orpheum", category="theatre", qid="Q7103475", sitelinks=7)
    facts = Facts(qid="Q7103475", classes=frozenset({"theatre building"}), heritage=True)
    worth = assess(poi, table=table, facts=facts)
    assert worth.mode is WorthMode.EVENT
    assert worth.score >= DEFAULT_CUT


def test_a_modern_stadium_with_an_architect_is_not_a_sight(table):
    """Comerica Park. Every modern stadium has a named architect, so P84 alone must not
    carry a bowl over the cut the way a heritage designation does."""
    poi = make_poi("Comerica Park", category="stadium", qid="Q868482", sitelinks=28, distance=0.9)
    facts = Facts(qid="Q868482", classes=frozenset({"baseball park"}), architect=True)
    listing = Listing(kind="do", name="Comerica Park", article="Detroit/Downtown", qid="Q868482")
    worth = assess(poi, table=table, listing=listing, facts=facts)
    assert worth.score < DEFAULT_CUT


def test_heritage_does_not_lift_infrastructure_on_its_own(table):
    """Pacific Central Station: heritage-listed, named architect, named style, a station.

    Those three bonuses alone carried it over the cut while nothing suggested anyone
    visits it.
    """
    poi = make_poi("Pacific Central Station", qid="Q1327751", sitelinks=15)
    facts = Facts(
        qid="Q1327751",
        classes=frozenset({"railway station", "historic building"}),
        heritage=True,
        architect=True,
        style=True,
    )
    worth = assess(poi, table=table, facts=facts)
    assert worth.mode is WorthMode.INCIDENTAL
    assert worth.score < DEFAULT_CUT
    assert "heritage" not in worth.evidence


def test_a_guide_listing_unlocks_heritage_for_infrastructure(table):
    """Michigan Central Station is the same class and genuinely is a sight."""
    poi = make_poi("Michigan Central Station", qid="Q1928672", sitelinks=16)
    facts = Facts(
        qid="Q1928672", classes=frozenset({"railway station"}), heritage=True, architect=True
    )
    listing = Listing(
        kind="see",
        name="Michigan Central Station",
        article="Detroit/Southwest Side",
        qid="Q1928672",
        content="Built in 1913, the station stood derelict for decades before reopening.",
    )
    worth = assess(poi, table=table, listing=listing, facts=facts)
    assert worth.score >= DEFAULT_CUT
    assert "heritage" in worth.evidence


def test_distance_never_penalises_a_recommended_place(table):
    """Grouse Mountain is 14 km out and in the oracle; the Heidelberg Project was cut
    at 4.93 by a distance penalty applied on top of a verified listing."""
    listing = Listing(kind="see", name="Grouse Mountain", article="North Vancouver")
    near = assess(make_poi("Grouse Mountain", category="nature", distance=1.0), table=table,
                  listing=listing)
    far = assess(make_poi("Grouse Mountain", category="nature", distance=14.0), table=table,
                 listing=listing)
    assert near.score == far.score


def test_distance_still_counts_against_the_unrecommended(table):
    near = assess(make_poi("Some Tower", distance=1.0), table=table)
    far = assess(make_poi("Some Tower", distance=21.0), table=table)
    assert far.score < near.score


def test_confidence_distinguishes_a_guide_from_a_class_prior(table):
    listing = Listing(kind="see", name="Stanley Park", article="Vancouver/West End")
    park = make_poi("Stanley Park", category="park")
    assert assess(park, table=table, listing=listing).confidence is Confidence.VERIFIED
    assert assess(park, table=table).confidence is Confidence.ASSUMED


def test_unknown_class_is_not_silently_scored_as_worthless(table):
    worth = assess(make_poi("Mystery Thing", category="nonexistent"), table=table)
    assert worth.mode is WorthMode.UNKNOWN
    assert worth.confidence is Confidence.UNKNOWN


def test_a_class_prior_alone_never_clears_the_cut(table):
    """A prior is not a verdict, and a prior at or above the cut is one.

    The artwork and museum priors were 5 against a 4.0 cut, so every named artwork was
    auto-included: Vancouver's shortlist reached 569 of which 291 were artworks,
    "We Found Love Crosswalk" and "Our Children Again Crosswalk" among them.
    """
    for category in ("artwork", "monument", "museum", "gallery", "park", "nature"):
        worth = assess(make_poi(f"Some {category}", category=category), table=table)
        assert worth.score < DEFAULT_CUT, f"{category} clears the cut on its prior alone"


def test_a_wikidata_entity_separates_a_sculpture_from_a_crosswalk(table):
    """375 Vancouver artworks carry 18 Wikidata entities between them.

    Somebody bothering to create one is the cheapest available evidence that a piece of
    public art is more than street furniture.
    """
    crosswalk = assess(make_poi("We Found Love Crosswalk", category="artwork"), table=table)
    sculpture = assess(
        make_poi("Monument to Joe Louis", category="artwork", qid="Q6899926", sitelinks=3),
        table=table,
    )
    assert crosswalk.score < DEFAULT_CUT
    assert sculpture.score >= DEFAULT_CUT
    assert "wikidata" in sculpture.evidence


def test_the_entity_bonus_does_not_reach_venues(table):
    """Every stadium has an entity, so for a venue it says nothing about the building —
    applying it there put Comerica Park back over the cut."""
    poi = make_poi("Comerica Park", category="stadium", qid="Q868482", sitelinks=28, distance=0.9)
    facts = Facts(qid="Q868482", classes=frozenset({"baseball park"}), architect=True)
    listing = Listing(kind="do", name="Comerica Park", article="Detroit/Downtown", qid="Q868482")
    worth = assess(poi, table=table, listing=listing, facts=facts)
    assert "wikidata" not in worth.evidence
    assert worth.score < DEFAULT_CUT


def test_reason_prefers_what_is_specific_to_this_place(table):
    """The class sentence is identical on every POI of its kind and sits above a tag
    already naming the category — all 17 shortlisted Vancouver artworks read "Made to be
    looked at." A Wikidata description is a weaker claim but is at least about the thing
    in front of you.
    """
    poi = make_poi("Digital Orca", category="artwork", qid="Q5276374")
    poi.description = "sculpture by Douglas Coupland"
    assert assess(poi, table=table).reason == "sculpture by Douglas Coupland"

    listing = Listing(kind="see", name="Digital Orca", article="Vancouver/City Centre",
                      content="A pixellated killer whale beside the convention centre.")
    guided = assess(poi, table=table, listing=listing)
    assert guided.reason == "A pixellated killer whale beside the convention centre."


def test_class_rationale_is_the_last_resort_only(table):
    """It still has a job when no source says anything — 2 POIs across both cities."""
    worth = assess(make_poi("Eve's Apple", category="artwork"), table=table)
    assert worth.reason == table.classify(frozenset(), {"artwork"}).why
    assert worth.confidence is Confidence.ASSUMED, "and the confidence field reports it"


# --- joining to the guide --------------------------------------------------------


def test_join_prefers_qid_over_name():
    poi = make_poi("Fairmont Hotel Vancouver", qid="Q930702")
    listings = [
        Listing(kind="see", name="Hotel Vancouver", article="Vancouver/City Centre", qid="Q930702"),
        Listing(kind="do", name="Fairmont Hotel Vancouver", article="Elsewhere"),
    ]
    assert join_listings([poi], listings)[poi.key].kind == "see"


def test_join_prefers_see_over_do_for_duplicate_names():
    """Wikivoyage repeats listings across a city article and its district pages."""
    poi = make_poi("Little Caesars Arena")
    listings = [
        Listing(kind="do", name="Little Caesars Arena", article="Detroit"),
        Listing(kind="see", name="Little Caesars Arena", article="Detroit/Midtown-New Center"),
    ]
    assert join_listings([poi], listings)[poi.key].kind == "see"


def test_join_will_not_match_a_distant_namesake():
    """Containment matching without a distance bound merges unrelated same-named places."""
    poi = make_poi("Central Park", lat=49.28, lon=-123.11)
    listings = [
        Listing(kind="see", name="Central Park", article="Somewhere", lat=40.78, lon=-73.96)
    ]
    assert join_listings([poi], listings) == {}


# --- the cut is a separate step --------------------------------------------------


def test_cut_marks_and_never_deletes(table):
    pois = [
        make_poi("Museum of Anthropology", category="museum", qid="Q1954088", osm_id=1),
        make_poi("Boundary Bay Airport", category="landmark", osm_id=2, distance=22.0),
    ]
    listing = Listing(kind="see", name="Museum of Anthropology", article="Vancouver/UBC")
    for poi in pois:
        poi.worth = assess(poi, table=table, listing=listing if poi.wikidata else None)
    kept = apply_cut(pois, threshold=DEFAULT_CUT)
    assert len(pois) == 2, "scoring and cutting must not remove anything"
    assert kept == 1
    assert pois[0].worth.shortlisted and not pois[1].worth.shortlisted


def test_cut_is_tunable_without_rescoring(table):
    poi = make_poi("Museum of Anthropology", category="museum")
    poi.worth = assess(poi, table=table)
    before = poi.worth.score
    apply_cut([poi], threshold=99.0)
    assert not poi.worth.shortlisted
    assert poi.worth.score == before, "the cut must not disturb the evidence behind it"


# --- wikitext parsing ------------------------------------------------------------


def test_listing_content_survives_a_piped_wiki_link():
    """The Little Caesars Arena listing embeds [[Ice hockey in North America|NHL]].

    Splitting the template on every pipe truncates the prose mid-sentence and drops
    every field after it, including the coordinates.
    """
    wikitext = (
        "{{see|name=Little Caesars Arena|lat=42.341|long=-83.055"
        "|content=Home of the [[Ice hockey in North America|NHL]] Red Wings.}}"
    )
    (listing,) = parse_listings(wikitext, "Detroit/Downtown")
    assert listing.content == "Home of the [[Ice hockey in North America|NHL]] Red Wings."
    assert listing.lat == pytest.approx(42.341)
    assert listing.summary == "Home of the NHL Red Wings."


def test_only_see_and_do_are_listings():
    """`{{listing}}` carries airlines, consulates and hotels — FlixBus is not a sight."""
    wikitext = "{{listing|name=FlixBus|content=Intercity coaches.}}{{see|name=Gastown}}"
    assert [listing.name for listing in parse_listings(wikitext, "Vancouver")] == ["Gastown"]
