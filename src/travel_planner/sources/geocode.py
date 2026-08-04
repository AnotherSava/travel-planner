"""Turning a destination string into a query boundary.

Two services, because one cannot do both jobs. Nominatim's usage policy forbids
autocomplete and caps at one request per second, so the typeahead uses Photon — the
OSM-based geocoder built for exactly that case — while Nominatim performs the
authoritative resolve.

The hard part is not the rate limit; R0 geocodes once per city and caches, so lifetime
volume is a few dozen requests. The hard part is that "the city" is ambiguous. Nominatim
returns two results both displaying exactly ``Lisboa, Portugal``: an 11.7 x 12.4 km city
relation and a 71.5 x 62.0 km one, thirty times the area. Naples repeats the pattern.
Picking the wrong one silently changes what the product is, so results are gated to
administrative relations of a city-like type and everything else is refused loudly.
"""

from __future__ import annotations

import re
import time
import unicodedata

import httpx

from travel_planner.model import Address, City

NOMINATIM_ENDPOINT = "https://nominatim.openstreetmap.org/search"
PHOTON_ENDPOINT = "https://photon.komoot.io/api"

USER_AGENT = "travel-planner/0.1 (+https://github.com/AnotherSava/travel-planner; POI discovery)"

# Anything broader than a municipality changes the product rather than widening it:
# a state or country boundary produces a candidate set no itinerary can use.
ACCEPTED_ADDRESS_TYPES = frozenset({"city", "town", "municipality", "borough"})

_NOMINATIM_MIN_INTERVAL = 1.1
_last_nominatim_call = 0.0


class GeocodeError(RuntimeError):
    pass


class AmbiguousPlaceError(GeocodeError):
    """The query matched several administrative areas with the same display name."""

    def __init__(self, query: str, candidates: list[dict]) -> None:
        self.candidates = candidates
        described = "; ".join(
            f"{c['display_name']} (osm relation {c['osm_id']}, {_span_km(c):.0f} km across)"
            for c in candidates
        )
        super().__init__(
            f"{query!r} matched {len(candidates)} administrative areas: {described}. "
            "Pick one explicitly by relation id."
        )


def slugify(value: str) -> str:
    ascii_form = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", ascii_form.lower()).strip("-")


def _span_km(result: dict) -> float:
    south, north, west, east = (float(v) for v in result["boundingbox"])
    return max(north - south, east - west) * 111.0


def _throttle() -> None:
    global _last_nominatim_call
    elapsed = time.monotonic() - _last_nominatim_call
    if elapsed < _NOMINATIM_MIN_INTERVAL:
        time.sleep(_NOMINATIM_MIN_INTERVAL - elapsed)
    _last_nominatim_call = time.monotonic()


def _get(client: httpx.Client, url: str, params: dict, *, attempts: int = 4) -> httpx.Response:
    """GET with backoff.

    Both services shed load by resetting the connection rather than returning a status,
    so a transport error is a normal operating condition here, not an exception. An
    unretried geocode is enough to fail a whole city build after the expensive work.
    """
    last: Exception | None = None
    for attempt in range(attempts):
        if attempt:
            time.sleep(min(2**attempt * 5, 60))
        try:
            reply = client.get(url, params=params)
        except httpx.HTTPError as exc:
            last = exc
            continue
        if reply.status_code in {429, 502, 503, 504}:
            last = GeocodeError(f"HTTP {reply.status_code} from {url}")
            continue
        reply.raise_for_status()
        return reply
    raise GeocodeError(f"{url} failed after {attempts} attempts") from last


def suggest(query: str, *, limit: int = 8, client: httpx.Client | None = None) -> list[dict]:
    """Typeahead candidates from Photon. Safe to call per keystroke; Nominatim is not."""
    owned = client is None
    client = client or httpx.Client(timeout=10.0, headers={"User-Agent": USER_AGENT})
    try:
        reply = _get(client, PHOTON_ENDPOINT, {"q": query, "limit": limit, "osm_tag": "place:city"})
        features = reply.json().get("features", [])
    finally:
        if owned:
            client.close()

    return [
        {
            "name": f["properties"].get("name"),
            "country": f["properties"].get("country"),
            "state": f["properties"].get("state"),
            "osm_type": f["properties"].get("osm_type"),
            "osm_id": f["properties"].get("osm_id"),
            "extent": f["properties"].get("extent"),
        }
        for f in features
    ]


def resolve(
    query: str,
    *,
    osm_id: int | None = None,
    client: httpx.Client | None = None,
) -> City:
    """Resolve a destination to a single administrative relation.

    Pass ``osm_id`` to disambiguate when the same display name maps to several areas.
    """
    owned = client is None
    client = client or httpx.Client(timeout=30.0, headers={"User-Agent": USER_AGENT})
    try:
        _throttle()
        reply = _get(
            client,
            NOMINATIM_ENDPOINT,
            {"q": query, "format": "jsonv2", "addressdetails": 1, "limit": 20},
        )
        results = reply.json()
    finally:
        if owned:
            client.close()

    if not results:
        raise GeocodeError(f"{query!r} matched nothing")

    # A node result is unusable: deriving an Overpass area id from one returns zero
    # elements with no error, and Nominatim's synthesised ~2 km box is not the place.
    candidates = [
        r
        for r in results
        if r.get("osm_type") == "relation" and r.get("addresstype") in ACCEPTED_ADDRESS_TYPES
    ]

    if osm_id is not None:
        chosen = next((r for r in candidates if r["osm_id"] == osm_id), None)
        if chosen is None:
            raise GeocodeError(f"relation {osm_id} is not among the matches for {query!r}")
    elif not candidates:
        types = sorted({f"{r.get('osm_type')}/{r.get('addresstype')}" for r in results})
        raise GeocodeError(
            f"{query!r} matched no city-like administrative relation (saw {', '.join(types)}). "
            "Regions, states and countries are not valid destinations."
        )
    elif len(candidates) > 1:
        raise AmbiguousPlaceError(query, candidates)
    else:
        chosen = candidates[0]

    address = chosen.get("address", {})

    # The slug carries its region because the bare name is not unique: Vancouver,
    # British Columbia and Vancouver, Washington are both addresstype=city, and a
    # fixture or dataset filename that cannot tell them apart silently overwrites one
    # city's data with another's.
    region = address.get("state") or address.get("county") or address.get("country")
    parts = [slugify(chosen["display_name"].split(",")[0])]
    if region:
        parts.append(slugify(region))

    return City(
        slug="-".join(parts),
        display_name=chosen["display_name"],
        osm_type=chosen["osm_type"],
        osm_id=chosen["osm_id"],
        address_type=chosen["addresstype"],
        centre_lat=float(chosen["lat"]),
        centre_lon=float(chosen["lon"]),
        address=Address(
            country=address.get("country"),
            country_code=address.get("country_code"),
            state=address.get("state"),
            county=address.get("county"),
            city=address.get("city") or address.get("town"),
            municipality=address.get("municipality"),
            raw=address,
        ),
    )
