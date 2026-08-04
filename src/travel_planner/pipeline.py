"""Turning an Overpass response into a ranked, deduplicated, mapped city dataset."""

from __future__ import annotations

import re
import unicodedata
from collections import defaultdict

from travel_planner.geometry import extent_km, haversine_km, normalise
from travel_planner.model import City, CityDataset, LabelSource, Poi, Ranking
from travel_planner.sources import wikidata
from travel_planner.sources.overpass import OverpassResponse, coalesce
from travel_planner.whitelist import Whitelist

# Two POIs closer than this with the same normalised name are the same thing. Measured
# on Lisbon, 22 same-name pairs sat within 150 m and most carried no QID at all.
DEDUP_RADIUS_M = 150.0

# Rank cut as a fraction of ranked POIs rather than an absolute sitelink threshold.
# Absolute cutoffs encode city fame, not POI quality: Paris's top POI has 189 sitelinks,
# Lisbon's 57, Vancouver's 32 and Detroit's 29. One constant cannot serve all four.
DEFAULT_RANK_PERCENTILE = 0.25

# How far from the centre still counts as "this city". This is the actual boundary of
# the product, replacing the municipal one: what matters is being close enough to reach
# and worth reaching, not which council collects the taxes. Measured needs — Capilano
# 11 km and Grouse 12 km from Vancouver; The Henry Ford 15.5 km and the Detroit Zoo
# 18 km from Detroit — all fall inside 25 km, while Cranbrook at 32 km is genuinely a
# separate day trip.
SEARCH_RADIUS_KM = 25.0
MAX_DISTANCE_KM = SEARCH_RADIUS_KM

# Natural features above this size are geography, not destinations. Measured extents in
# Vancouver: Burrard Peninsula 38.8 km, Burrard Inlet 31.2 km, the three harbours 7-9 km,
# English Bay 4.4 km, False Creek 3.1 km — against Granville Island at 0.5 km and Coal
# Harbour at 0.9 km. A pin for a 31 km inlet has nowhere honest to sit, which is why
# these were the features whose markers landed on dry land.
#
# The cap applies only to `nature`, because size does not disqualify anything else: a
# large park is still somewhere you go. Pacific Spirit Regional Park spans 5.2 km and
# belongs on the map; Burrard Inlet does not.
MAX_NATURE_EXTENT_KM = 2.5

# Safety valve for the geometry pass, not a judgement about destinations. Anything this
# large is a coastline or an administrative boundary whose member geometry runs to
# megabytes by itself; everything a city actually shows sits far below it.
MAX_FETCH_EXTENT_KM = 30.0

_ZERO_WIDTH = re.compile(r"[​-‏﻿]")


def clean_name(value: str) -> str:
    """Strip invisible characters that break exact-match dedup and title joins.

    'Paroisse Saint-Sacrement' in the Lisbon data begins with a zero-width space.
    """
    return _ZERO_WIDTH.sub("", value).strip()


def _fold(value: str) -> str:
    folded = unicodedata.normalize("NFKD", clean_name(value)).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", " ", folded.lower()).strip()


def is_latin(value: str) -> bool:
    """Whether a string is already readable without transliteration."""
    letters = [c for c in value if c.isalpha()]
    if not letters:
        return True
    return all("LATIN" in unicodedata.name(c, "") for c in letters)


def _latin_label(tags: dict[str, str], entity: wikidata.Entity | None) -> tuple[str, LabelSource]:
    """Resolve a displayable Latin-script name.

    Inert in English-speaking cities, where it falls straight through to ``name``. It
    exists for the case measured in central Kyoto, where 3.7% of names are ASCII and
    65.7% have no Latin-script label available from any tag.

    A local name that is *already* Latin-script wins over every substitute. Preferring
    the Wikidata label unconditionally renamed Vancouver's "Marker of Change" memorial
    to "École Polytechnique massacre" — the QID describes the 1989 event the memorial
    commemorates, and that mislabelling survives the P625 coordinate gate because the
    event itself has a location.
    """
    name = clean_name(tags.get("name", ""))
    if name and is_latin(name):
        return name, LabelSource.LOCAL
    if name_en := tags.get("name:en"):
        return clean_name(name_en), LabelSource.NAME_EN
    if entity and entity.label:
        return clean_name(entity.label), LabelSource.WIKIDATA
    if int_name := tags.get("int_name"):
        return clean_name(int_name), LabelSource.INT_NAME
    if romaji := tags.get("name:ja_rm"):
        return clean_name(romaji), LabelSource.ROMAJI
    return name, LabelSource.LOCAL


def _select(element: dict, whitelist: Whitelist) -> tuple[str, list[str]] | None:
    """Category and matched families, from either selection path."""
    tags = element.get("tags", {})
    if not tags.get("name"):
        return None

    matched = whitelist.matched_categories(tags)
    if matched:
        return whitelist.category(matched), matched
    if whitelist.prominence.eligible(tags):
        return "landmark", ["prominence"]
    return None


def _dedup(pois: list[Poi]) -> list[Poi]:
    """Collapse duplicates, preferring name+coordinate over QID.

    The priority is deliberately the reverse of the obvious one. Measured in Lisbon,
    13 QIDs sat on more than one element (2.9%) against 30 names repeating across 65
    elements — and most of the repeats carried no QID, so a QID-first rule would do
    almost none of the work.

    QID merging is also unsafe on its own: one Lisbon QID spans a cable-car line and
    both its stations, another a viewpoint and its adjacent garden, a third the species
    Tipuana tipu sitting on three separate street trees. So a shared QID only merges
    when the elements are also close together.
    """
    buckets: dict[str, list[Poi]] = defaultdict(list)
    for poi in pois:
        buckets[_fold(poi.label)].append(poi)

    survivors: list[Poi] = []
    for group in buckets.values():
        clusters: list[list[Poi]] = []
        for poi in group:
            for cluster in clusters:
                if any(
                    haversine_km(poi.lat, poi.lon, other.lat, other.lon) * 1000 < DEDUP_RADIUS_M
                    for other in cluster
                ):
                    cluster.append(poi)
                    break
            else:
                clusters.append([poi])
        survivors.extend(_merge(cluster) for cluster in clusters)

    return survivors


def _merge(cluster: list[Poi]) -> Poi:
    """Pick the survivor by rule, never by iteration order.

    Ordering by iteration would let a cable-car line's pin inherit the name of one of
    its stations. Prefer the richest record: ranked over unranked, then a reliable
    coordinate, then the longest label.
    """
    if len(cluster) == 1:
        return cluster[0]

    best = max(
        cluster,
        key=lambda p: (
            p.sitelinks or -1,
            p.coordinate_source.value != "bbox_centre",
            len(p.label),
        ),
    )
    merged = best.model_copy(deep=True)
    for other in cluster:
        merged.wikidata = merged.wikidata or other.wikidata
        merged.wikipedia = merged.wikipedia or other.wikipedia
        merged.description = merged.description or other.description
        merged.image = merged.image or other.image
        merged.website = merged.website or other.website
        for family in other.matched:
            if family not in merged.matched:
                merged.matched.append(family)
    return merged


def relations_needing_geometry(
    elements: list[dict],
    whitelist: Whitelist,
    *,
    max_nature_extent_km: float = MAX_NATURE_EXTENT_KM,
    max_fetch_extent_km: float = MAX_FETCH_EXTENT_KM,
) -> list[int]:
    """Relation ids worth a second, geometry-fetching pass.

    The rule is simply: **if it will be shown, place it properly.** So the only features
    excluded here are the ones the pipeline itself drops.

    These were previously bounded by ``MAX_NATURE_EXTENT_KM``, which conflated two
    unrelated questions — "is this too big to be a destination" and "is this too
    expensive to fetch". Pacific Spirit Regional Park failed the second while passing the
    first, so it stayed on the map with a bounding-box centre for no reason: at 5.2 km it
    is no harder to place than False Creek at 3.1 km, which was dropped outright.
    """
    ids = []
    for element in elements:
        if element.get("type") != "relation":
            continue
        tags = element.get("tags", {})
        matched = whitelist.matched_categories(tags)
        if not (matched or whitelist.prominence.eligible(tags)):
            continue

        span = extent_km(element)
        # Mirror the pipeline's own drop rule so the two cannot drift apart.
        if (
            whitelist.category(matched) == "nature"
            and span is not None
            and span > max_nature_extent_km
        ):
            continue
        # A safety valve only: a single relation spanning this far is a coastline or an
        # administrative boundary whose geometry runs to megabytes on its own.
        if span is not None and span > max_fetch_extent_km:
            continue
        ids.append(element["id"])
    return ids


def build(
    response: OverpassResponse,
    city: City,
    *,
    whitelist: Whitelist | None = None,
    rank_percentile: float = DEFAULT_RANK_PERCENTILE,
    max_distance_km: float = MAX_DISTANCE_KM,
    max_nature_extent_km: float = MAX_NATURE_EXTENT_KM,
    relation_geometry: dict[int, list[dict]] | None = None,
    enrich: bool = True,
) -> CityDataset:
    whitelist = whitelist or Whitelist.load()

    elements = coalesce(response.elements)
    if relation_geometry:
        for element in elements:
            if (geometry := relation_geometry.get(element["id"])) and element["type"] == "relation":
                element["members"] = geometry

    candidates: list[tuple[dict, str, list[str]]] = []
    for element in elements:
        # The city's own boundary relation matches the prominence path and outranks
        # everything inside it.
        if element.get("type") == "relation" and element.get("id") == city.osm_id:
            continue
        if (selection := _select(element, whitelist)) is not None:
            candidates.append((element, *selection))

    qids = {
        e.get("tags", {})["wikidata"] for e, _, _ in candidates if "wikidata" in e.get("tags", {})
    }
    entities = wikidata.fetch(sorted(qids)) if enrich and qids else {}

    pois: list[Poi] = []
    for element, category, matched in candidates:
        tags = element.get("tags", {})
        coordinate = normalise(element)
        if coordinate is None:
            continue

        entity = entities.get(tags.get("wikidata", ""))

        # An element selected on fame alone must actually clear the fame bar.
        if matched == ["prominence"] and (
            entity is None or entity.sitelinks < whitelist.prominence.min_sitelinks
        ):
            continue

        distance_km = haversine_km(city.centre_lat, city.centre_lon, coordinate.lat, coordinate.lon)
        if distance_km > max_distance_km:
            continue

        span_km = extent_km(element)
        if category == "nature" and span_km is not None and span_km > max_nature_extent_km:
            continue

        label, label_source = _latin_label(tags, entity)

        pois.append(
            Poi(
                osm_type=element["type"],
                osm_id=element["id"],
                name=clean_name(tags.get("name", "")),
                label=label,
                label_source=label_source,
                lat=coordinate.lat,
                lon=coordinate.lon,
                coordinate_source=coordinate.source,
                category=category,
                matched=list(matched),
                wikidata=tags.get("wikidata"),
                wikipedia=tags.get("wikipedia"),
                sitelinks=entity.sitelinks if entity else None,
                ranking=Ranking.SITELINKS if entity else Ranking.UNRANKED,
                description=(entity.description if entity else None) or tags.get("description"),
                image=entity.image if entity else None,
                website=tags.get("website"),
                distance_from_centre_km=round(distance_km, 2),
                extent_km=round(span_km, 3) if span_km is not None else None,
                tags=tags,
            )
        )

    pois = _dedup(pois)
    pois.sort(key=lambda p: (-(p.sitelinks or -1), p.label))

    ranked = [p for p in pois if p.ranking is Ranking.SITELINKS]
    if ranked and 0 < rank_percentile < 1:
        keep = max(1, int(len(ranked) * rank_percentile))
        floor = ranked[keep - 1].sitelinks or 0
        for poi in ranked[keep:]:
            if (poi.sitelinks or 0) < floor:
                poi.ranking = Ranking.UNRANKED

    return CityDataset(
        city=city,
        generated_from_extract=response.extract_timestamp.isoformat(),
        pois=pois,
    )
