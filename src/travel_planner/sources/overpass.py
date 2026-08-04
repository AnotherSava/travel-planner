"""Overpass API client.

Overpass does not fail loudly. Every guard in this module exists because of a
specific observed failure mode:

- A query that exceeds its timeout returns HTTP 200, valid JSON, ``"elements": []``,
  and a ``remark`` key. Nothing else signals the failure.
- A partially completed query returns *partial* elements plus the same ``remark``,
  so the response looks like plausible data.
- An area id derived from a node rather than a relation returns HTTP 200 with zero
  results and no error at all.
- Overload returns HTTP 504 with an XHTML body, which breaks JSON parsing before any
  status check runs.
- Mirrors serve stale extracts non-deterministically: the same mirror returned data
  five weeks old on one call and seven weeks old minutes later.

Any of these written into the per-city cache poisons it permanently, so a response
is validated before it is returned and a rejected response is never cached.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx

DEFAULT_ENDPOINT = "https://overpass-api.de/api/interpreter"

# Identifies this application per the Overpass usage policy. A library default or a
# browser-impersonating string is a policy violation.
USER_AGENT = "travel-planner/0.1 (+https://github.com/AnotherSava/travel-planner; POI discovery)"

# Extracts older than this are treated as a mirror serving stale data.
MAX_EXTRACT_AGE = timedelta(days=7)

RETRY_STATUSES = frozenset({429, 502, 503, 504})


class OverpassError(RuntimeError):
    """A response that must not be cached or used."""


class OverpassStaleError(OverpassError):
    """The endpoint served an extract older than MAX_EXTRACT_AGE."""


class OverpassEmptyError(OverpassError):
    """The query succeeded at the HTTP level but produced no usable elements."""


@dataclass(frozen=True, slots=True)
class OverpassResponse:
    """A validated Overpass response.

    ``raw`` is retained verbatim so it can be frozen as a test fixture — the
    acceptance gate runs against fixtures because Overpass has no version pinning
    and a live gate cannot distinguish an upstream edit from a local regression.
    """

    elements: list[dict]
    extract_timestamp: datetime
    raw: dict

    @property
    def age(self) -> timedelta:
        return datetime.now(UTC) - self.extract_timestamp


def _parse_extract_timestamp(payload: dict) -> datetime:
    stamp = payload.get("osm3s", {}).get("timestamp_osm_base")
    if not stamp:
        raise OverpassError("response carries no osm3s.timestamp_osm_base; provenance unknown")
    return datetime.fromisoformat(stamp.replace("Z", "+00:00"))


def relation_geometry_query(ids: list[int], *, timeout: int = 180) -> str:
    """Fetch member geometry for specific relations.

    Plain ``out geom``, deliberately without the ``tags`` modifier — that modifier means
    *tags only* and suppresses member listing, which is why relations came back with no
    geometry in the main query.

    Run over the relations that survive selection rather than all of them: for the
    sub-kilometre features that need a precise point this costs 60-90 KB per city, while
    the handful of kilometre-scale ones cost 1.3-2.8 MB between them.
    """
    return f"[out:json][timeout:{timeout}];relation(id:{','.join(map(str, ids))});out geom;"


def coalesce(elements: list[dict]) -> list[dict]:
    """Merge the duplicate emissions produced by multiple ``out`` statements.

    The query asks for geometry and centres in two separate statements, because
    combining them into ``out center geom;`` silently drops the centres. Each element
    therefore appears once per statement, carrying a different subset of position
    fields. First value for a key wins, so ``normalise``'s existing precedence
    (geometry over centre) still decides which position is used.
    """
    merged: dict[tuple[str, int], dict] = {}
    for element in elements:
        key = (element["type"], element["id"])
        if (existing := merged.get(key)) is None:
            merged[key] = dict(element)
        else:
            existing.update({k: v for k, v in element.items() if k not in existing})
    return list(merged.values())


def validate(payload: dict, *, allow_empty: bool = False) -> OverpassResponse:
    """Reject the silent failures. Raises OverpassError rather than returning junk."""
    remark = payload.get("remark")
    if remark:
        # Present on both total and *partial* failures, so this check must come first.
        raise OverpassError(f"overpass remark: {remark}")

    elements = payload.get("elements")
    if elements is None:
        raise OverpassError("response has no 'elements' key")

    timestamp = _parse_extract_timestamp(payload)
    response = OverpassResponse(elements=elements, extract_timestamp=timestamp, raw=payload)

    if response.age > MAX_EXTRACT_AGE:
        raise OverpassStaleError(
            f"extract is {response.age.days} days old (timestamp {timestamp.isoformat()}); "
            "the endpoint is serving a stale mirror"
        )
    if not elements and not allow_empty:
        raise OverpassEmptyError("query returned zero elements")

    return response


class OverpassClient:
    """Queries a single pinned endpoint.

    Deliberately does not fail over to mirrors: mirror staleness is invisible and
    non-deterministic, so a silent failover trades a loud error for wrong data.
    """

    def __init__(
        self,
        endpoint: str = DEFAULT_ENDPOINT,
        *,
        timeout: float = 300.0,
        max_retries: int = 4,
        cache_dir: Path | None = None,
    ) -> None:
        self.endpoint = endpoint
        self.max_retries = max_retries
        self.cache_dir = cache_dir
        self._client = httpx.Client(
            timeout=timeout,
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> OverpassClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def query(self, ql: str, *, cache_key: str | None = None) -> OverpassResponse:
        """Run a query, validating before anything is cached or returned."""
        if cache_key and (cached := self._read_cache(cache_key)) is not None:
            return cached

        payload = self._fetch(ql)
        response = validate(payload)

        if cache_key:
            self._write_cache(cache_key, payload)
        return response

    def _fetch(self, ql: str) -> dict:
        last_error: Exception | None = None

        for attempt in range(self.max_retries):
            if attempt:
                # The public instance allows two concurrent slots per IP and sheds load
                # well before that; observed recovery is 30-120s.
                time.sleep(min(2**attempt * 5, 120))

            try:
                reply = self._client.post(self.endpoint, data={"data": ql})
            except httpx.HTTPError as exc:
                last_error = exc
                continue

            if reply.status_code in RETRY_STATUSES:
                last_error = OverpassError(f"HTTP {reply.status_code} from {self.endpoint}")
                continue

            reply.raise_for_status()

            try:
                return reply.json()
            except json.JSONDecodeError as exc:
                # 504 arrives as an XHTML body; parsing fails before any status check.
                snippet = reply.text[:200].replace("\n", " ")
                raise OverpassError(
                    f"non-JSON body from {self.endpoint} (HTTP {reply.status_code}): {snippet}"
                ) from exc

        raise OverpassError(
            f"{self.endpoint} failed after {self.max_retries} attempts"
        ) from last_error

    def _cache_path(self, key: str) -> Path | None:
        return None if self.cache_dir is None else self.cache_dir / f"{key}.json"

    def _read_cache(self, key: str) -> OverpassResponse | None:
        path = self._cache_path(key)
        if path is None or not path.exists():
            return None
        try:
            # Cached payloads are re-validated: staleness is a function of wall-clock
            # time, so a response that was fresh when written may not be now.
            return validate(json.loads(path.read_text()))
        except (OverpassError, json.JSONDecodeError):
            return None

    def _write_cache(self, key: str, payload: dict) -> None:
        path = self._cache_path(key)
        if path is None:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=1))
