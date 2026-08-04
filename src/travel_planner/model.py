"""The records R0 emits.

R0 writes ``cities/<slug>.json`` and a static frontend reads it. There is no HTTP
service until R1, where ``resolve_hours(poi, date)`` gives the backend genuine
per-request work — so these models exist to be serialised to disk and diffed in
review, not to be an API schema.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field

from travel_planner.geometry import CoordinateSource


class LabelSource(StrEnum):
    """Where a POI's Latin-script label came from.

    Needed because the fallback chain degrades in quality. In central Kyoto only 3.7%
    of ``name`` values are pure ASCII and 65.7% have no Latin-script label from any
    tag at all, so an English-speaking user otherwise gets a list they cannot read,
    search, or match against a guidebook.
    """

    NAME_EN = "name:en"
    WIKIDATA = "wikidata_label"
    INT_NAME = "int_name"
    ROMAJI = "romaji"
    LOCAL = "local"
    """No Latin-script label exists; the local name is shown as-is."""


class Ranking(StrEnum):
    """Why a POI holds the rank it does."""

    SITELINKS = "sitelinks"
    UNRANKED = "unranked"
    """No Wikidata QID, so no prominence signal. Half of a typical city."""


class WorthMode(StrEnum):
    """How a place earns a visit — or whether it does at all.

    Ranking says whether somewhere is *known*; this says whether there is anything
    to look at. The two come apart badly in practice: Vancouver's sitelink order puts
    an airport, a neighbouring city, two city halls and a public library above
    Stanley Park.
    """

    INTRINSIC = "intrinsic"
    """The thing itself is the sight. Turn up on any open day and it is there."""
    EVENT = "event"
    """A container. BC Place is a stadium; the stadium is not the attraction, the
    game is. A real destination that needs a date attached, which R1 supplies."""
    INCIDENTAL = "incidental"
    """Infrastructure or civic function. Visited for a purpose, never for the sight.
    The only mode that means "there is nothing to see"."""
    UNKNOWN = "unknown"
    """No class evidence from either Wikidata or the OSM category."""


class Confidence(StrEnum):
    """How far a derived fact is to be trusted.

    "Nothing to see" and "we do not know" must never render identically — one is a
    finding, the other is ignorance, and conflating them is what makes a
    recommendation untrustworthy.
    """

    VERIFIED = "verified"
    """A travel guide states it directly."""
    INFERRED = "inferred"
    """Derived from a structured class plus corroborating evidence."""
    ASSUMED = "assumed"
    """A class prior alone."""
    UNKNOWN = "unknown"


class Worth(BaseModel):
    """Why a place is worth a visit, and how much.

    Carries its own evidence list because the score is a sum of priors and signals
    that will be retuned per city, exactly like the whitelist. A bare number is not
    reviewable; a number next to ``["guide:see", "heritage", "class:museum"]`` is.
    """

    mode: WorthMode
    score: float
    confidence: Confidence
    reason: str
    """One line on why to go — the guide's own sentence where one exists."""
    detail: str | None = None
    """The guide's full entry, when there is one.

    Kept alongside ``reason`` because a guidebook's opening sentence is often
    scene-setting rather than the hook: the Hotel Vancouver's begins "The third such
    hotel in this spot, built over 11 years and completed in 1939" and ends "Bring
    binoculars for a close-up view of the building's many gargoyles". The short form
    fits a list row; the long form is what actually answers "why go".
    """
    evidence: list[str] = Field(default_factory=list)
    guide_kind: str | None = None
    """``see`` or ``do``, when a Wikivoyage listing was matched."""
    guide_article: str | None = None
    """Attribution for ``reason`` when it came from Wikivoyage (CC BY-SA)."""
    source: str | None = None
    """Where ``reason`` and ``detail`` came from: ``wikivoyage``, ``wikipedia``,
    ``wikidata`` or ``class``. Both wikis are CC BY-SA and need attributing."""
    shortlisted: bool = False
    """Set by the cut, which is a separate step — scoring never drops anything."""


class Address(BaseModel):
    """The geocoder's full address object, stored rather than reduced to a scalar.

    R1's holiday calendar needs municipality granularity in at least some countries:
    Nominatim populates ``state`` for US, Italian and British results but leaves it
    empty for Portuguese sub-city ones, and Portugal's relevant closure driver is the
    municipal holiday. Re-geocoding every cached city later to recover a discarded
    field is a self-inflicted migration, so the whole object is kept.
    """

    country: str | None = None
    country_code: str | None = None
    state: str | None = None
    county: str | None = None
    city: str | None = None
    municipality: str | None = None
    raw: dict = Field(default_factory=dict)


class City(BaseModel):
    """A resolved query boundary.

    ``osm_type`` is always ``relation``: an area id derived from a node returns HTTP
    200 with zero results and no error, and Nominatim synthesises a ~2 km box around
    node results that is not the place anyone means.
    """

    slug: str
    display_name: str
    osm_type: str
    osm_id: int
    address_type: str
    centre_lat: float
    centre_lon: float
    address: Address


class Poi(BaseModel):
    osm_type: str
    osm_id: int

    name: str
    """The local-language name, always kept — it is what the signage says."""
    label: str
    """The Latin-script name to display."""
    label_source: LabelSource

    lat: float
    lon: float
    coordinate_source: CoordinateSource

    category: str
    """Resolved through the precedence table; an element can match several families."""
    matched: list[str] = Field(default_factory=list)
    """Every whitelist predicate this element satisfied, for auditing the category."""

    wikidata: str | None = None
    wikipedia: str | None = None
    sitelinks: int | None = None
    ranking: Ranking = Ranking.UNRANKED

    description: str | None = None
    image: str | None = None
    website: str | None = None

    distance_from_centre_km: float
    extent_km: float | None = None
    """Longer side of the bounding box. Separates destinations from geography."""

    worth: Worth | None = None
    """Populated by the worth step, which runs after ``build`` on a written dataset."""

    tags: dict[str, str] = Field(default_factory=dict)

    @property
    def key(self) -> str:
        return f"{self.osm_type}/{self.osm_id}"


class CityDataset(BaseModel):
    """One ``cities/<slug>.json``."""

    city: City
    generated_from_extract: str
    """The Overpass ``timestamp_osm_base`` the data came from."""
    pois: list[Poi]

    @property
    def ranked(self) -> list[Poi]:
        return [p for p in self.pois if p.ranking is Ranking.SITELINKS]
