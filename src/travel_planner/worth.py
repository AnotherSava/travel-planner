"""Why a place is worth a visit — the step between "what is it" and "when is it open".

R0 answers *what* a thing is and *how known* it is. Neither answers what a visitor
asks. "BC Place is a stadium at Vancouver, British Columbia" is a complete, correct,
useless sentence: it does not say whether there is anything to look at, and there is
not — unless there is a game or a show on.

This module supplies the missing judgement without a model, because a model in the
runtime pipeline would trade a deterministic, diffable artifact for a per-run API
cost and a gate that cannot distinguish a prompt change from a data change. The
substitute is four structured signals, each measured against the two test cities:

- **Class**, from Wikidata ``P31`` and this project's own OSM categories, separates a
  thing that *is* a sight from a container for events from plain infrastructure.
- **A travel guide's attention**, from Wikivoyage ``{{see}}``/``{{do}}`` listings. The
  sharpest signal available: of 22 Vancouver POIs that rank high on sitelinks and are
  not sightseeing, 18 have no listing at all. It also supplies the actual sentence
  explaining why to go.
- **Wikipedia lead extracts**, for the two thirds of the shortlist no guide wrote up.
  90% of Vancouver's un-guided shortlist and 70% of Detroit's have an English article,
  against Wikidata descriptions that are routinely tautologies — "St. Andrew's Wesley
  Church" described as "church building".
- **Formal recognition of the fabric**, from ``P1435`` heritage designation and the
  weaker ``P84``/``P149``. This is what separates the Guardian Building from One Wall
  Centre — both are ``skyscraper`` to Wikidata, and only one is a landmark. Note it is
  effectively a proxy for age: 0 of 34 POIs built since 2000 carry a designation.

One source was measured and rejected: **Commons photo density fails.** Counting
geotagged photographs within 300 m scores BC Place and Rogers Arena at the 500-result
cap, level with Granville Island, while Stanley Park scores 7 — its pole of
inaccessibility sits deep in the trees where nobody stands. It measures how built-up
a place is, not whether anything there is worth a photograph.

Scoring and cutting are **two steps**. ``assess`` scores every POI and drops nothing;
``apply_cut`` marks those above a threshold. Keeping them apart means the judgement
and the policy are independently reviewable, and a threshold change never destroys
the evidence that justified it.
"""

from __future__ import annotations

import math
import re
import tomllib
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from travel_planner.model import Confidence, Poi, Worth, WorthMode
from travel_planner.sources.wikidata import Facts
from travel_planner.sources.wikipedia import Article
from travel_planner.sources.wikivoyage import Listing

WORTH_PATH = Path(__file__).parent / "worth.toml"

# A listing and a POI describing the same place can sit a long way apart even when
# both are right: a guide pins a park's entrance while the pipeline pins its pole of
# inaccessibility, which for Stanley Park is deep in the forest.
#
# Two radii, because the two name paths carry different risk. An exact name match is
# already strong evidence, so it gets a generous bound whose only job is to reject a
# different city's namesake — the article set spans a whole metropolitan area, so
# "Central Park" and "City Hall" are live hazards. A partial match is much weaker and
# stays tight.
NAME_MATCH_RADIUS_KM = 10.0
PARTIAL_MATCH_RADIUS_KM = 0.5

# Where the cut falls by default. Keeps 174 of 2,190 Vancouver POIs and 150 of 1,545
# for Detroit, with every oracle entry surviving except Greenfield Village.
#
# The threshold matters less than the priors beneath it. It was unchanged at 4.0 while
# the shortlist ran to 569 and 335, three quarters of it POIs scoring 4.0-4.9 on a class
# prior and nothing else — 291 Vancouver artworks, painted crosswalks included. That was
# a weights bug, not a threshold one: an intrinsic prior of 5 against a 4.0 cut is a
# verdict. Raising the cut instead would have cost oracle entries while leaving the
# padding proportionally intact.
#
# Greenfield Village is the one accepted loss at 2.11: no QID, no guide listing, and no
# heritage record and no Wikipedia article, so no signal reaches it at all.
#
# Overridable per run, because it is a policy rather than a judgement — `--cut`.
DEFAULT_CUT = 4.0


@lru_cache(maxsize=4096)
def _phrase(token: str) -> re.Pattern[str]:
    return re.compile(rf"\b{re.escape(token)}\b")


def _contains_phrase(label: str, token: str) -> bool:
    return _phrase(token).search(label) is not None


@dataclass(frozen=True, slots=True)
class WorthClass:
    mode: WorthMode
    weight: float
    why: str
    wikidata: frozenset[str] = frozenset()
    osm: frozenset[str] = frozenset()

    def match_strength(self, label: str) -> int:
        """Length of the longest table token found in *one* class label; 0 for no match.

        Whole-phrase, not equality. Wikidata does not use the tidy class names a lookup
        table wants: New Westminster is not ``P31 city`` but ``city in British
        Columbia``, and an equality test let a whole municipality fall through to its
        OSM category and score as a monument. Phrase matching is also what makes the
        table portable — one ``city`` entry covers every ``city in <region>`` variant
        the next country produces.

        The *length* is returned rather than a boolean so that competing tokens inside
        a single label resolve to the more specific one: ``baseball park`` contains the
        whole word ``park``, and without this a ballpark classifies as a park.

        Scoped to one label deliberately. Ranking whole entities by token length
        instead compares statements about *different* aspects of a place and picks the
        wordiest — which read Grouse Mountain as a ``ski resort`` rather than a
        ``mountain``, and the Detroit Institute of Arts as a ``performing arts center``
        rather than an ``art museum``. Across labels it is mode precedence that decides.

        Word boundaries still matter — ``park`` must not match ``parking garage``.
        """
        return max(
            (len(token) for token in self.wikidata if _contains_phrase(label, token)),
            default=0,
        )

    def matches_osm(self, categories: set[str]) -> bool:
        return bool(self.osm & categories)


@dataclass(frozen=True, slots=True)
class Evidence:
    wikidata_entity: float = 0.0
    guide_see: float = 0.0
    guide_do: float = 0.0
    guide_event: float = 0.0
    heritage_designation: float = 0.0
    architect: float = 0.0
    architectural_style: float = 0.0
    sitelinks_per_point: float = 0.0
    sitelinks_cap: float = 0.0
    distance_penalty_per_km: float = 0.0
    distance_penalty_cap: float = 0.0


@dataclass(frozen=True, slots=True)
class WorthTable:
    classes: tuple[WorthClass, ...]
    mode_precedence: tuple[str, ...]
    evidence: Evidence = field(default_factory=Evidence)

    @classmethod
    def load(cls, path: Path = WORTH_PATH) -> WorthTable:
        raw = tomllib.loads(path.read_text())
        return cls(
            classes=tuple(
                WorthClass(
                    mode=WorthMode(entry["mode"]),
                    weight=float(entry["weight"]),
                    why=" ".join(entry["why"].split()),
                    wikidata=frozenset(v.lower() for v in entry.get("wikidata", ())),
                    osm=frozenset(entry.get("osm", ())),
                )
                for entry in raw["class"]
            ),
            mode_precedence=tuple(raw["mode_precedence"]),
            evidence=Evidence(**raw.get("evidence", {})),
        )

    def classify(self, classes: frozenset[str], categories: set[str]) -> WorthClass | None:
        """Resolve every matching class to one.

        **A Wikidata class beats an OSM category outright, rather than competing with
        it on precedence.** The two are not equal evidence: ``P31`` is a claim about
        this specific object, while the OSM category is the bucket a tag predicate
        dropped it in. Letting them compete produced exactly the failures this step
        exists to fix — New Westminster is ``P31 city`` but carries a monument-ish tag,
        so an intrinsic-first precedence scored a whole municipality as a sight; the
        Fairmont Hotel is ``P31 hotel`` and reached intrinsic the same way.

        Resolution runs in **two stages, and the order matters**. Each class label is
        first resolved on its own to the most specific table entry matching it, so
        ``baseball park`` reaches the event entry rather than the intrinsic ``park``
        one. Only then do the surviving labels compete, and there it is mode precedence
        that decides — because a place legitimately carries several labels describing
        different aspects of it, and the wordiest is not the truest. Grouse Mountain is
        ``mountain`` and ``ski resort``; the Detroit Institute of Arts is ``art
        museum``, ``museum`` and ``performing arts center``. Intrinsic must win both.

        Multi-matches are normal, since an element arrives carrying every tag it has.
        Rejecting anything matching ``event`` would delete the Fox Theatre, a Detroit
        oracle entry.
        """
        per_label = [self._most_specific(label) for label in classes]
        if by_qid := [c for c in per_label if c is not None]:
            return self._resolve(by_qid)
        if by_osm := [c for c in self.classes if c.matches_osm(categories)]:
            return self._resolve(by_osm)
        return None

    def _most_specific(self, label: str) -> WorthClass | None:
        """The table entry matching this one label by its longest token."""
        best: WorthClass | None = None
        longest = 0
        for candidate in self.classes:
            if (strength := candidate.match_strength(label)) > longest:
                longest, best = strength, candidate
        return best

    def _resolve(self, matched: list[WorthClass]) -> WorthClass:
        return min(
            matched,
            key=lambda c: (
                self.mode_precedence.index(c.mode)
                if c.mode in self.mode_precedence
                else len(self.mode_precedence),
                -c.weight,
            ),
        )


def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    radius = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dlat, dlon = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dlat / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlon / 2) ** 2
    return 2 * radius * math.asin(math.sqrt(a))


def _fold(value: str) -> str:
    return "".join(c for c in value.lower() if c.isalnum() or c.isspace()).strip()


def _within(poi: Poi, listing: Listing, radius_km: float) -> bool:
    """Whether the two are close enough to be the same place.

    A listing with no coordinate cannot be checked, so it passes: 13-26% of listings
    carry no ``lat``/``long``, and rejecting all of them would discard the guide's
    judgement over a missing field it never promised.
    """
    if listing.lat is None or listing.lon is None:
        return True
    return _haversine_km(poi.lat, poi.lon, listing.lat, listing.lon) < radius_km


def join_listings(pois: list[Poi], listings: list[Listing]) -> dict[str, Listing]:
    """Match POIs to guide listings, QID first.

    The priority is the reverse of ``pipeline._dedup``'s, and for the opposite reason.
    There, both sides were OSM elements and QIDs were sparse and sometimes shared
    across unrelated features. Here both sides are curated by hand and 67-80% of
    listings carry a ``wikidata=`` parameter, so the QID is the reliable key and names
    are the fallback — the guide writes "Hotel Vancouver" where OSM has "Fairmont
    Hotel Vancouver", and "Bloedel Conservatory" for a POI inside Queen Elizabeth Park.

    A ``see`` listing wins over a ``do`` listing for the same POI: Little Caesars Arena
    appears twice in the Detroit articles under two different QIDs, and Wikivoyage
    duplicates listings across a city article and its district pages.
    """
    by_qid = {listing.qid: listing for listing in listings if listing.qid}
    by_name: dict[str, Listing] = {}
    for listing in listings:
        key = _fold(listing.name)
        if key not in by_name or listing.kind == "see":
            by_name[key] = listing

    located = [x for x in listings if x.lat is not None and x.lon is not None]
    matched: dict[str, Listing] = {}

    for poi in pois:
        if poi.wikidata and (hit := by_qid.get(poi.wikidata)) is not None:
            matched[poi.key] = hit
            continue

        label = _fold(poi.label)
        if (hit := by_name.get(label)) is not None and _within(poi, hit, NAME_MATCH_RADIUS_KM):
            matched[poi.key] = hit
            continue

        # A containment relationship still counts — the guide lists "Bloedel
        # Conservatory", the pipeline has "Bloedel Floral Conservatory" — but only
        # when the two are also close enough to be the same thing.
        for listing in located:
            name = _fold(listing.name)
            if not name or (name not in label and label not in name):
                continue
            if _within(poi, listing, PARTIAL_MATCH_RADIUS_KM):
                matched[poi.key] = listing
                break

    return matched


def _describe(
    poi: Poi,
    listing: Listing | None,
    article: Article | None,
    worth_class: WorthClass | None,
) -> tuple[str, str | None, str]:
    """The reason line, the long form, and which source they came from.

    Ordered by how specific each source is to *this* place. The class rationale comes
    last and only when nothing else exists, because it is a statement about a category
    being read as a statement about a place: it is identical on every POI of its kind —
    all 17 artworks in the Vancouver shortlist said "Made to be looked at." — and it
    sits directly above a tag already naming that category, so it manages to be vacuous
    and redundant at once.

    A Wikidata description ranks below Wikipedia for the same reason. It is specific to
    the POI but routinely tautological: "St. Andrew's Wesley Church" is described as
    "church building" and Point Atkinson Lighthouse as "lighthouse", which the name
    already said.

    Coverage makes the ordering nearly free. Of shortlisted POIs with no guide listing,
    90% of Vancouver's and 70% of Detroit's have an English Wikipedia article, which
    leaves the class fallback firing on 2 POIs across both cities.

    Nothing here manufactures a reason. When no source has anything to say, the
    confidence field is what reports that, and it already reads ``assumed``.
    """
    if listing is not None and (summary := listing.summary):
        return summary, listing.prose, "wikivoyage"
    if article is not None and (summary := article.summary):
        return summary, article.extract, "wikipedia"
    if description := (poi.description or "").strip():
        return description, None, "wikidata"
    if worth_class is not None:
        return worth_class.why, None, "class"
    return "Nothing is recorded about what there is to see here.", None, "class"


def assess(
    poi: Poi,
    *,
    table: WorthTable,
    listing: Listing | None = None,
    facts: Facts | None = None,
    article: Article | None = None,
) -> Worth:
    """Score one POI. Never drops anything — the cut is a separate step."""
    classes = facts.classes if facts else frozenset()
    categories = {poi.category, *poi.matched}
    worth_class = table.classify(classes, categories)
    evidence: list[str] = []

    score = worth_class.weight if worth_class else 0.0
    if worth_class:
        evidence.append(f"class:{worth_class.mode.value}:{worth_class.weight:g}")

    # Somebody created a Wikidata entity for this. Weak alone, decisive for street
    # furniture: 375 Vancouver artworks carry 18 entities between them, so this is what
    # separates a public sculpture from a painted crosswalk. Intrinsic only — every
    # stadium has an entity, and for a venue that says nothing about the building.
    if poi.wikidata and worth_class is not None and worth_class.mode is WorthMode.INTRINSIC:
        score += table.evidence.wikidata_entity
        evidence.append("wikidata")

    if listing is not None:
        # A guide listing on an event venue is evidence about the *programme*, not the
        # building, so it buys confidence and the reason line but no score. Without
        # this, class 3 + do 3.5 cleared a 4.0 cut before any evidence was considered,
        # and every guide-listed venue was shortlisted — Swangard Stadium at 7.2 on the
        # strength of hosting the BC High School Championships.
        if worth_class is not None and worth_class.mode is WorthMode.EVENT:
            weight = table.evidence.guide_event
        elif listing.kind == "see":
            weight = table.evidence.guide_see
        else:
            weight = table.evidence.guide_do
        score += weight
        evidence.append(f"guide:{listing.kind}")

    # Recognition of the fabric lifts a place that is *somewhere you go*. It does not
    # convert infrastructure into a destination: Pacific Central Station is a heritage
    # building with a named architect and a named style, and it is a working railway
    # station nobody visits — those three bonuses alone carried it over the cut.
    # Michigan Central Station is the same class and genuinely is a sight, and it has a
    # guide listing saying so. So a guide listing is what unlocks this, for incidentals.
    recognised = worth_class is None or worth_class.mode is not WorthMode.INCIDENTAL
    if facts is not None and (recognised or listing is not None):
        if facts.heritage:
            score += table.evidence.heritage_designation
            evidence.append("heritage")
        if facts.architect:
            score += table.evidence.architect
            evidence.append("architect")
        if facts.style:
            score += table.evidence.architectural_style
            evidence.append("style")

    if poi.sitelinks:
        bonus = min(
            poi.sitelinks * table.evidence.sitelinks_per_point, table.evidence.sitelinks_cap
        )
        score += bonus
        evidence.append(f"sitelinks:{poi.sitelinks}")

    # Distance counts against prominence, never against a recommendation. The penalty
    # exists to stop a suburban civic building outranking a central attraction on
    # sitelinks alone; applying it to a place a guide explicitly wrote up would demote
    # Grouse Mountain for being on a mountain. It also pushed the Heidelberg Project
    # below the cut at 4.93 despite a verified listing.
    if listing is None:
        penalty = min(
            poi.distance_from_centre_km * table.evidence.distance_penalty_per_km,
            table.evidence.distance_penalty_cap,
        )
        if penalty:
            score -= penalty
            evidence.append(f"distance:-{penalty:.2f}")

    if listing is not None:
        confidence = Confidence.VERIFIED
    elif worth_class is not None and (facts and (facts.heritage or facts.architect)):
        confidence = Confidence.INFERRED
    elif worth_class is not None:
        confidence = Confidence.ASSUMED
    else:
        confidence = Confidence.UNKNOWN

    reason, detail, source = _describe(poi, listing, article, worth_class)
    return Worth(
        mode=worth_class.mode if worth_class else WorthMode.UNKNOWN,
        score=round(max(score, 0.0), 2),
        confidence=confidence,
        reason=reason,
        detail=detail,
        source=source,
        evidence=evidence,
        guide_kind=listing.kind if listing else None,
        guide_article=listing.article if listing else None,
    )


def score_dataset(
    pois: list[Poi],
    *,
    listings: list[Listing],
    facts: dict[str, Facts],
    articles: dict[str, Article] | None = None,
    table: WorthTable | None = None,
) -> None:
    """Attach a ``Worth`` to every POI, in place."""
    table = table or WorthTable.load()
    matched = join_listings(pois, listings)
    articles = articles or {}
    for poi in pois:
        poi.worth = assess(
            poi,
            table=table,
            listing=matched.get(poi.key),
            facts=facts.get(poi.wikidata or ""),
            article=articles.get(poi.key),
        )


def apply_cut(pois: list[Poi], *, threshold: float = DEFAULT_CUT) -> int:
    """Mark everything at or above the threshold, and return how many survived.

    A separate step from scoring, and a marking rather than a deletion. Both choices
    are the same choice the ranking cut already makes: the recall gate can only
    measure what is still in the file, and a dropped POI is an unrecoverable
    judgement made on a signal that is explicitly incomplete.
    """
    kept = 0
    for poi in pois:
        if poi.worth is None:
            continue
        poi.worth.shortlisted = poi.worth.score >= threshold
        kept += poi.worth.shortlisted
    return kept
