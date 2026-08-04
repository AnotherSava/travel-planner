"""Coordinate normalisation, including the cases where `out center` lies."""

from __future__ import annotations

from shapely.geometry import Point, Polygon

from travel_planner.geometry import CoordinateSource, normalise


def ring(points: list[tuple[float, float]]) -> list[dict]:
    """Closed way geometry from (lat, lon) pairs."""
    closed = [*points, points[0]]
    return [{"lat": lat, "lon": lon} for lat, lon in closed]


def encloses(geometry: list[dict], coord) -> bool:
    """Whether a coordinate falls inside a closed way."""
    polygon = Polygon([(p["lon"], p["lat"]) for p in geometry])
    return polygon.contains(Point(coord.lon, coord.lat))


# A C-shaped polygon: its bounding-box centre sits in the open mouth, outside the
# feature. This is the shape behind the 7.9% of Lisbon pins that missed their own
# polygon — parks with a concave boundary, palaces built around a courtyard.
C_SHAPE = ring(
    [
        (0.0, 0.0),
        (0.0, 10.0),
        (10.0, 10.0),
        (10.0, 8.0),
        (2.0, 8.0),
        (2.0, 2.0),
        (10.0, 2.0),
        (10.0, 0.0),
    ]
)


def test_node_coordinates_are_used_directly():
    coord = normalise({"type": "node", "lat": 45.5, "lon": -122.6})
    assert (coord.lat, coord.lon) == (45.5, -122.6)
    assert coord.source is CoordinateSource.NODE


def test_closed_way_gets_a_point_inside_itself():
    coord = normalise({"type": "way", "geometry": C_SHAPE})
    assert coord.source in {CoordinateSource.POLYLABEL, CoordinateSource.CENTROID}
    assert encloses(C_SHAPE, coord)


def test_bbox_centre_of_that_shape_would_have_been_outside():
    # Establishes that the previous test is testing something real: the coordinate
    # `out center` supplies for this feature falls outside it.
    bbox_centre = normalise({"type": "way", "center": {"lat": 5.0, "lon": 5.0}})
    assert bbox_centre.source is CoordinateSource.BBOX_CENTRE
    assert not encloses(C_SHAPE, bbox_centre)


def test_open_way_falls_back_to_geometry_mean():
    line = [{"lat": 0.0, "lon": 0.0}, {"lat": 2.0, "lon": 4.0}]
    coord = normalise({"type": "way", "geometry": line})
    assert (coord.lat, coord.lon) == (1.0, 2.0)
    assert coord.source is CoordinateSource.GEOMETRY_MEAN


def test_geometry_wins_over_center_when_both_present():
    # `out center geom` returns geometry and no centre, so geometry must be tried
    # first; this pins the precedence so adding `geom` cannot silently change pins.
    element = {"type": "way", "geometry": C_SHAPE, "center": {"lat": 5.0, "lon": 5.0}}
    assert normalise(element).source is not CoordinateSource.BBOX_CENTRE


def test_center_is_used_when_nothing_better_exists():
    coord = normalise({"type": "relation", "center": {"lat": 49.28, "lon": -123.12}})
    assert (coord.lat, coord.lon) == (49.28, -123.12)
    assert coord.source is CoordinateSource.BBOX_CENTRE


def test_element_with_no_position_returns_none():
    assert normalise({"type": "relation", "id": 7}) is None
