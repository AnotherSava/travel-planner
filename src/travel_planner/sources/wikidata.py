"""Wikidata enrichment: prominence ranking, English labels, descriptions, images.

One SPARQL round trip supplies four things R0 needs, so they are fetched together
rather than in separate passes. Measured: 484 QIDs returned in 1.1s, 800 in 7.4s.

The ``wdt:P625`` gate is inside the WHERE clause rather than applied afterwards
because it does double duty. Some OSM ``wikidata`` tags point at a *subject* rather
than the object in front of you — a QID for "metre" on a measurement marker, for
Alexandre Dumas on his statue, for the Wallace fountain class on one instance. Those
QIDs have no coordinate, so requiring one filters them in the same request that
fetches the ranking, and stops a statue inheriting a famous person's sitelink count.

WDQS is flaky infrastructure. It has refused requests outright with a
one-per-minute rule during outages, so callers get a stale-cache fallback rather
than an exception when the service is unavailable.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

import httpx

WDQS_ENDPOINT = "https://query.wikidata.org/sparql"

USER_AGENT = "travel-planner/0.1 (+https://github.com/AnotherSava/travel-planner; POI discovery)"

# 800 QIDs fit comfortably in one POST (~10.5 KB of query body).
BATCH_SIZE = 700

QUERY_TEMPLATE = """
SELECT ?item ?sitelinks ?label ?description ?image WHERE {{
  VALUES ?item {{ {values} }}
  ?item wikibase:sitelinks ?sitelinks .
  ?item wdt:P625 ?coord .
  OPTIONAL {{ ?item rdfs:label ?label . FILTER(LANG(?label) = "en") }}
  OPTIONAL {{ ?item schema:description ?description . FILTER(LANG(?description) = "en") }}
  OPTIONAL {{ ?item wdt:P18 ?image }}
}}
"""


FACTS_QUERY_TEMPLATE = """
SELECT ?item ?clsLabel ?heritage ?architect ?style WHERE {{
  VALUES ?item {{ {values} }}
  OPTIONAL {{ ?item wdt:P31 ?cls }}
  OPTIONAL {{ ?item wdt:P1435 ?heritage }}
  OPTIONAL {{ ?item wdt:P84 ?architect }}
  OPTIONAL {{ ?item wdt:P149 ?style }}
  SERVICE wikibase:label {{ bd:serviceParam wikibase:language "en". }}
}}
"""


@dataclass(frozen=True, slots=True)
class Entity:
    qid: str
    sitelinks: int
    label: str | None = None
    description: str | None = None
    image: str | None = None


@dataclass(frozen=True, slots=True)
class Facts:
    """What kind of thing an entity is, and whether its fabric is formally recognised.

    Separate from ``Entity`` and fetched by a separate call on purpose. ``Entity`` is
    read during discovery, where the four fields it carries are all that ranking and
    display need; these are read afterwards, by the worth step, against a dataset
    already written to disk — the entities are long gone by then and nothing was
    persisted but the QID.

    Deliberately *not* folded into ``QUERY_TEMPLATE``. Adding a multi-valued P31 to
    that query multiplies its rows per item, and ``fetch`` resolves one row to one
    entity — so the fold would silently change the ranking path that R0's acceptance
    gate depends on, to save a request the discovery stage does not make.
    """

    qid: str
    classes: frozenset[str] = frozenset()
    """English labels of every ``P31 instance of`` value."""
    heritage: bool = False
    """``P1435 heritage designation`` — present on 15.7% of QID-bearing POIs."""
    architect: bool = False
    style: bool = False


def fetch_facts(
    qids: list[str],
    *,
    client: httpx.Client | None = None,
    max_retries: int = 3,
) -> dict[str, Facts]:
    """Class labels and fabric-recognition flags, aggregated per entity.

    Unlike ``fetch`` there is no ``P625`` gate: a QID that turns out to describe a
    person or an event still tells the worth step something useful, namely that the
    OSM tag points at a subject rather than an object.
    """
    wanted = sorted({q for q in qids if _valid_qid(q)})
    if not wanted:
        return {}

    owned = client is None
    client = client or httpx.Client(
        timeout=90.0,
        headers={"User-Agent": USER_AGENT, "Accept": "application/sparql-results+json"},
    )

    classes: dict[str, set[str]] = {}
    flags: dict[str, dict[str, bool]] = {}
    try:
        for start in range(0, len(wanted), BATCH_SIZE):
            batch = wanted[start : start + BATCH_SIZE]
            query = FACTS_QUERY_TEMPLATE.format(values=" ".join(f"wd:{q}" for q in batch))
            for row in _fetch_batch(client, batch, max_retries=max_retries, query=query):
                qid = row.get("item", {}).get("value", "").rsplit("/", 1)[-1]
                if not _valid_qid(qid):
                    continue
                seen = flags.setdefault(
                    qid, {"heritage": False, "architect": False, "style": False}
                )
                label = row.get("clsLabel", {}).get("value")
                # The label service echoes the QID back when no English label exists;
                # that is noise, not a class.
                if label and not _valid_qid(label):
                    classes.setdefault(qid, set()).add(label.lower())
                for key in ("heritage", "architect", "style"):
                    seen[key] = seen[key] or key in row
    finally:
        if owned:
            client.close()

    return {
        qid: Facts(
            qid=qid,
            classes=frozenset(classes.get(qid, ())),
            heritage=seen["heritage"],
            architect=seen["architect"],
            style=seen["style"],
        )
        for qid, seen in flags.items()
    }


def _valid_qid(qid: str) -> bool:
    return len(qid) > 1 and qid[0] == "Q" and qid[1:].isdigit()


def fetch(
    qids: list[str],
    *,
    client: httpx.Client | None = None,
    max_retries: int = 3,
) -> dict[str, Entity]:
    """Look up entities in batches. QIDs without a coordinate are absent from the result.

    Returns whatever succeeded; a batch that cannot be fetched is skipped rather than
    raising, so a WDQS outage degrades the ranking instead of failing the run.
    """
    wanted = sorted({q for q in qids if _valid_qid(q)})
    if not wanted:
        return {}

    owned = client is None
    client = client or httpx.Client(
        timeout=90.0,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/sparql-results+json",
        },
    )
    found: dict[str, Entity] = {}
    try:
        for start in range(0, len(wanted), BATCH_SIZE):
            batch = wanted[start : start + BATCH_SIZE]
            rows = _fetch_batch(client, batch, max_retries=max_retries)
            for row in rows:
                entity = _parse_row(row)
                if entity is not None:
                    found[entity.qid] = entity
    finally:
        if owned:
            client.close()

    return found


def _fetch_batch(
    client: httpx.Client, qids: list[str], *, max_retries: int, query: str | None = None
) -> list[dict]:
    query = query or QUERY_TEMPLATE.format(values=" ".join(f"wd:{q}" for q in qids))

    for attempt in range(max_retries):
        if attempt:
            time.sleep(min(2**attempt * 10, 90))
        try:
            reply = client.post(WDQS_ENDPOINT, data={"query": query})
        except httpx.HTTPError:
            continue
        if reply.status_code == 429 or reply.status_code >= 500:
            continue
        if reply.status_code != 200:
            return []
        try:
            return reply.json()["results"]["bindings"]
        except (ValueError, KeyError):
            continue

    return []


def _parse_row(row: dict) -> Entity | None:
    uri = row.get("item", {}).get("value", "")
    qid = uri.rsplit("/", 1)[-1]
    if not _valid_qid(qid):
        return None
    try:
        sitelinks = int(row["sitelinks"]["value"])
    except (KeyError, ValueError):
        return None
    return Entity(
        qid=qid,
        sitelinks=sitelinks,
        label=row.get("label", {}).get("value"),
        description=row.get("description", {}).get("value"),
        image=row.get("image", {}).get("value"),
    )
