"""Coordinate normalisation for Overpass elements.

Two thirds of a typical city pull are ways and relations, which carry no top-level
``lat``/``lon`` — and that two thirds is where the parks, monasteries, castles and
monuments live. Requesting ``out center`` supplies a ``center`` object instead, so
every read site would otherwise need a dual-shape accessor. Normalising once at
ingest is what stops two thirds of the city being silently dropped.

``out center`` also returns the *bounding-box* centre rather than a point inside the
feature. Measured on 316 closed named Lisbon ways, 25 of them (7.9%) placed their own
pin outside their own polygon. On a product whose pitch is "and where is it", that is
visible to a user in the first minute and invisible in the JSON, so closed ways get a
real representative point instead.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import StrEnum

from shapely.geometry import LineString, Point, Polygon
from shapely.ops import linemerge, polygonize, polylabel


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    radius = 6371.0088
    dlat, dlon = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    a = (
        math.sin(dlat / 2) ** 2
        + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon / 2) ** 2
    )
    return 2 * radius * math.asin(math.sqrt(a))


class CoordinateSource(StrEnum):
    """How a coordinate was derived. Kept on the POI so pin quality is auditable."""

    NODE = "node"
    """Top-level lat/lon on a node. Exact."""

    POLYLABEL = "polylabel"
    """Pole of inaccessibility of a closed way. Guaranteed inside the feature."""

    CENTROID = "centroid"
    """Centroid of a closed way, used when polylabel fails. Usually inside."""

    GEOMETRY_MEAN = "geometry_mean"
    """Mean of an open way's nodes. Used for linear features."""

    BBOX_CENTRE = "bbox_centre"
    """Overpass 'center'. May fall outside the feature; last resort."""


@dataclass(frozen=True, slots=True)
class Coordinate:
    lat: float
    lon: float
    source: CoordinateSource


def _closed(geometry: list[dict]) -> bool:
    return len(geometry) >= 4 and geometry[0] == geometry[-1]


def _polygon(geometry: list[dict]) -> Polygon | None:
    try:
        polygon = Polygon([(p["lon"], p["lat"]) for p in geometry])
    except (KeyError, ValueError):
        return None
    if not polygon.is_valid:
        repaired = polygon.buffer(0)
        # buffer(0) on a self-intersecting ring often returns a MultiPolygon; keeping
        # only plain Polygons here silently discarded those features.
        if hasattr(repaired, "geoms"):
            parts = [g for g in repaired.geoms if isinstance(g, Polygon) and not g.is_empty]
            repaired = max(parts, key=lambda g: g.area) if parts else repaired
        polygon = repaired
    return polygon if isinstance(polygon, Polygon) and not polygon.is_empty else None


def _representative_point(polygon: Polygon) -> tuple[Point, CoordinateSource]:
    """Pole of inaccessibility, falling back to a guaranteed-inside centroid.

    Shapely's ``representative_point`` is the fallback rather than ``centroid``
    because a centroid can land outside a concave or ring-shaped polygon — which is
    exactly the failure ``out center`` already exhibits.
    """
    try:
        # tolerance scales with feature size so small features stay cheap
        tolerance = max(polygon.bounds[2] - polygon.bounds[0], 1e-5) / 100
        return polylabel(polygon, tolerance=tolerance), CoordinateSource.POLYLABEL
    except (ValueError, TypeError):
        return polygon.representative_point(), CoordinateSource.CENTROID


def _relation_polygon(members: list[dict]) -> Polygon | None:
    """Assemble a polygon from a relation's member ways.

    A multipolygon's outer ring is routinely split across several member ways that only
    form a closed ring once stitched end to end — Vancouver's Coal Harbour is four outer
    ways, none of them closed alone. Concatenating their points in listed order produces
    a self-intersecting mess, so the ways are merged as linework and polygonised, which
    is the operation that actually handles split and out-of-order rings.
    """
    outers = [m["geometry"] for m in members if m.get("role") == "outer" and m.get("geometry")]
    if not outers:
        outers = [m["geometry"] for m in members if m.get("geometry")]
    if not outers:
        return None

    lines = [LineString([(p["lon"], p["lat"]) for p in ring]) for ring in outers if len(ring) >= 2]
    if not lines:
        return None

    merged = linemerge(lines) if len(lines) > 1 else lines[0]
    if polygons := [p for p in polygonize(merged) if not p.is_empty]:
        # A relation can hold several islands; the largest is the one worth pinning.
        return max(polygons, key=lambda p: p.area)

    # Unclosed linework — a route or a riverbank rather than an area.
    return None


def _line_midpoint(members: list[dict]) -> tuple[float, float] | None:
    """Point halfway along a route relation's linework.

    Route relations (a race circuit, a ferry line) are lines, not areas: Detroit's Grand
    Prix relation has 34 members with roles like ``pit_lane`` and ``start-finish``. There
    is no interior to find a point inside, so the midpoint of the merged line is used.
    """
    lines = [
        LineString([(p["lon"], p["lat"]) for p in geometry])
        for m in members
        if (geometry := m.get("geometry")) and len(geometry) >= 2
    ]
    if not lines:
        return None
    merged = linemerge(lines) if len(lines) > 1 else lines[0]
    longest = max(merged.geoms, key=lambda g: g.length) if hasattr(merged, "geoms") else merged
    point = longest.interpolate(0.5, normalized=True)
    return point.y, point.x


def _member_node_mean(members: list[dict]) -> tuple[float, float] | None:
    """Mean of a relation's node members.

    Station relations list their platforms and entrances as nodes, which arrive with
    ``lat``/``lon`` rather than ``geometry`` — Vancouver's Commercial-Broadway station is
    five node members and no ways at all.
    """
    points = [(m["lat"], m["lon"]) for m in members if m.get("lat") is not None]
    if not points:
        return None
    return sum(p[0] for p in points) / len(points), sum(p[1] for p in points) / len(points)


def normalise(element: dict) -> Coordinate | None:
    """Reduce any Overpass element shape to one coordinate, or None if it has none.

    Note that ``out center geom`` returns geometry and *no* centre — the later output
    mode wins — so geometry is tried before ``center`` rather than after. A developer
    adding ``geom`` for debugging would otherwise silently lose every coordinate.
    """
    if (lat := element.get("lat")) is not None and (lon := element.get("lon")) is not None:
        return Coordinate(lat=lat, lon=lon, source=CoordinateSource.NODE)

    geometry = element.get("geometry")
    if geometry:
        if _closed(geometry) and (polygon := _polygon(geometry)) is not None:
            point, source = _representative_point(polygon)
            return Coordinate(lat=point.y, lon=point.x, source=source)

        points = [(p["lat"], p["lon"]) for p in geometry if "lat" in p and "lon" in p]
        if points:
            return Coordinate(
                lat=sum(p[0] for p in points) / len(points),
                lon=sum(p[1] for p in points) / len(points),
                source=CoordinateSource.GEOMETRY_MEAN,
            )

    # Relations carry their shape in member geometries rather than a top-level list.
    # Areas, then routes, then node-only relations — each needs a different reduction.
    if members := element.get("members"):
        if (polygon := _relation_polygon(members)) is not None:
            point, source = _representative_point(polygon)
            return Coordinate(lat=point.y, lon=point.x, source=source)
        if (midpoint := _line_midpoint(members)) is not None:
            return Coordinate(
                lat=midpoint[0], lon=midpoint[1], source=CoordinateSource.GEOMETRY_MEAN
            )
        if (mean := _member_node_mean(members)) is not None:
            return Coordinate(lat=mean[0], lon=mean[1], source=CoordinateSource.GEOMETRY_MEAN)

    if (centre := element.get("center")) is not None:
        return Coordinate(lat=centre["lat"], lon=centre["lon"], source=CoordinateSource.BBOX_CENTRE)

    # Relations arrive with `bounds` rather than geometry: expanding their members would
    # take one city's response from 2.4 MB to 12 MB, and the features that make it
    # expensive are the kilometre-scale ones that should not be pins at all. Sub-kilometre
    # relations — a hotel, a museum, a station — have bbox centres that sit inside them.
    if (bounds := element.get("bounds")) is not None:
        return Coordinate(
            lat=(bounds["minlat"] + bounds["maxlat"]) / 2,
            lon=(bounds["minlon"] + bounds["maxlon"]) / 2,
            source=CoordinateSource.BBOX_CENTRE,
        )

    return None


def extent_km(element: dict) -> float | None:
    """The longer side of an element's bounding box.

    Separates destinations from geography: Burrard Peninsula spans 38 km and Burrard
    Inlet 31 km, while a hotel or museum relation spans about 100 m. Available for free
    from `out bb`, which costs the same as `out center`.
    """
    if (bounds := element.get("bounds")) is not None:
        south, north = bounds["minlat"], bounds["maxlat"]
        west, east = bounds["minlon"], bounds["maxlon"]
    elif geometry := element.get("geometry"):
        lats = [p["lat"] for p in geometry if "lat" in p]
        lons = [p["lon"] for p in geometry if "lon" in p]
        if not lats:
            return None
        south, north, west, east = min(lats), max(lats), min(lons), max(lons)
    else:
        return None

    return max(
        haversine_km(south, west, north, west),
        haversine_km(south, west, south, east),
    )
