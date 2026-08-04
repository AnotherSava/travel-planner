"""Wikivoyage listings: what a travel guide says is worth seeing, and why.

Every other source in this project describes what a thing *is*. Wikidata will say
"BC Place is a stadium at Vancouver"; OSM will say ``leisure=stadium``. Neither
answers the question a visitor actually asks, which is whether there is anything
to look at. Wikivoyage is the only free source that answers it directly, because
its editors are writing a guide rather than a database: a place earns a ``{{see}}``
or ``{{do}}`` listing only when somebody thought it was worth a paragraph.

Measured across the two test cities, that editorial judgement is sharp. Of 22
Vancouver POIs that rank high on sitelinks but are not sightseeing — the airport,
two city halls, a public library, three universities, four transit stations, two
suburban bridges, Pacific Coliseum, Richmond Olympic Oval — **18 have no listing at
all**. Nothing in the tag data separates those from the real attractions; a travel
guide's silence does.

What it is *not* is a filter. Coverage is roughly 270 listings against 2,190 POIs,
and the gaps are not random: a listing exists where an editor happened to write one.
So absence is weak evidence and presence is strong evidence, which is why this
module supplies a signal and never a verdict.

**Articles are found by reach, not by name.** The same rule the Overpass query
follows applies here for the same reason: Wikivoyage splits a metropolitan area
across separate articles per municipality, so fetching only ``Vancouver`` loses
Capilano Suspension Bridge and Grouse Mountain to ``North Vancouver``, and fetching
only ``Detroit`` loses The Henry Ford to ``Dearborn``, the Detroit Zoo to
``Royal Oak`` and Cranbrook to ``Bloomfield Hills``. A geosearch grid over the
search radius recovers all of them.
"""

from __future__ import annotations

import json
import math
import re
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from travel_planner.sources.mediawiki import MediaWikiClient, MediaWikiError, first_sentence

API = "https://en.wikivoyage.org/w/api.php"

# The Wikimedia geosearch API caps a single request at 10 km, so covering a 25 km
# reach takes a grid of overlapping discs rather than one call. Cells are spaced
# below the cap so the discs overlap and leave no gap between rings.
GEOSEARCH_MAX_RADIUS_M = 10_000
GRID_CELL_KM = 9.0

# Wikimedia asks for serialised, unhurried access from a single client.
REQUEST_INTERVAL_S = 0.12

# Only these two templates carry sightseeing judgement. Wikivoyage also uses
# `{{listing}}` for airlines, bus operators, consulates and hotels, and `{{marker}}`
# for map pins with no prose — including both put FlixBus and the Albanian consulate
# in the results, which is the opposite of the signal wanted here.
LISTING_TEMPLATES = ("see", "do")


class WikivoyageError(MediaWikiError):
    """A response that must not be cached or used."""


@dataclass(frozen=True, slots=True)
class Listing:
    """One ``{{see}}`` or ``{{do}}`` entry from a Wikivoyage article.

    ``kind`` carries more meaning than it appears to. Wikivoyage's own taxonomy
    splits a thing you look at from a thing you attend, and its editors apply the
    split to exactly the case this project struggles with: Detroit's Ford Field,
    Little Caesars Arena and Comerica Park are all ``do``, never ``see``.
    """

    kind: str
    name: str
    article: str
    qid: str | None = None
    lat: float | None = None
    lon: float | None = None
    content: str = ""

    @property
    def prose(self) -> str:
        """The whole entry, cleaned of wiki markup."""
        return _strip_markup(self.content)

    @property
    def summary(self) -> str:
        """The first sentence of the guide's prose, cleaned of wiki markup."""
        return first_sentence(self.prose)


def _geosearch_grid(
    lat: float, lon: float, *, reach_km: float, cell_km: float = GRID_CELL_KM
) -> list[tuple[float, float]]:
    """Points whose 10 km geosearch discs cover a circle of ``reach_km``.

    Concentric rings rather than a square lattice: a square grid over a circular
    reach spends about a third of its requests on corners that the pipeline's own
    haversine filter discards anyway.
    """
    points = [(lat, lon)]
    ring = 1
    while (ring - 0.5) * cell_km < reach_km:
        radius = ring * cell_km
        count = max(6, int(2 * math.pi * radius / cell_km))
        for index in range(count):
            angle = 2 * math.pi * index / count
            points.append(
                (
                    lat + (radius * math.cos(angle)) / 111.32,
                    lon + (radius * math.sin(angle)) / (111.32 * math.cos(math.radians(lat))),
                )
            )
        ring += 1
    return points


def _split_template_fields(body: str) -> dict[str, str]:
    """Split a template body on its *top-level* pipes.

    Listing prose routinely contains nested templates and wiki links carrying their
    own pipes — ``[[Ice hockey in North America|NHL]]`` inside the Little Caesars
    Arena listing — so a plain ``str.split('|')`` truncates the content field
    mid-sentence and drops every field after it.
    """
    parts: list[str] = []
    depth = 0
    current = ""
    for index, char in enumerate(body):
        if body.startswith(("{{", "[["), index):
            depth += 1
        if body.startswith(("}}", "]]"), index):
            depth -= 1
        if char == "|" and depth <= 0:
            parts.append(current)
            current = ""
        else:
            current += char
    parts.append(current)

    fields: dict[str, str] = {}
    for part in parts:
        key, sep, value = part.partition("=")
        if sep:
            fields[key.strip().lower()] = value.strip()
    return fields


def parse_listings(wikitext: str, article: str) -> list[Listing]:
    """Extract ``{{see}}`` and ``{{do}}`` entries from one article's wikitext."""
    pattern = re.compile(r"\{\{\s*(" + "|".join(LISTING_TEMPLATES) + r")\s*\|", re.IGNORECASE)
    listings: list[Listing] = []
    cursor = 0

    while (match := pattern.search(wikitext, cursor)) is not None:
        # Walk to the matching close brace; listings nest other templates freely.
        depth = 2
        end = match.end()
        while end < len(wikitext) and depth:
            if wikitext.startswith("{{", end):
                depth += 2
                end += 2
            elif wikitext.startswith("}}", end):
                depth -= 2
                end += 2
            else:
                end += 1

        fields = _split_template_fields(wikitext[match.end() : end - 2])
        cursor = end

        if not (name := fields.get("name", "").strip()):
            continue

        qid = fields.get("wikidata", "").strip()
        listings.append(
            Listing(
                kind=match.group(1).lower(),
                name=_strip_markup(name),
                article=article,
                qid=qid if re.fullmatch(r"Q\d+", qid) else None,
                lat=_number(fields.get("lat")),
                lon=_number(fields.get("long")),
                content=fields.get("content", ""),
            )
        )
    return listings


def _number(value: str | None) -> float | None:
    try:
        return float(value) if value else None
    except ValueError:
        return None


_LINK = re.compile(r"\[\[(?:[^\]\|]*\|)?([^\]\|]+)\]\]")
_TEMPLATE = re.compile(r"\{\{[^{}]*\}\}")
_BOLD_ITALIC = re.compile(r"'{2,5}")
_TAG = re.compile(r"<[^>]+>")


def _strip_markup(value: str) -> str:
    """Reduce wikitext to plain prose.

    Links keep their display text: the Little Caesars Arena listing renders as
    "home for the Detroit Red Wings of the NHL", not as a target-and-pipe pair.
    """
    text = _LINK.sub(r"\1", value)
    text = _TEMPLATE.sub("", text)
    text = _TAG.sub("", text)
    text = _BOLD_ITALIC.sub("", text)
    return re.sub(r"\s+", " ", text).strip()




class WikivoyageClient(MediaWikiClient):
    endpoint = API
    error_type = WikivoyageError

    def articles_near(self, lat: float, lon: float, *, reach_km: float) -> list[str]:
        """Every Wikivoyage article whose coordinate falls within the reach."""
        titles: set[str] = set()
        for point_lat, point_lon in _geosearch_grid(lat, lon, reach_km=reach_km):
            payload = self.get(
                {
                    "action": "query",
                    "list": "geosearch",
                    "gscoord": f"{point_lat}|{point_lon}",
                    "gsradius": GEOSEARCH_MAX_RADIUS_M,
                    "gslimit": 500,
                }
            )
            for hit in payload.get("query", {}).get("geosearch", []):
                titles.add(hit["title"])
            time.sleep(REQUEST_INTERVAL_S)
        return sorted(titles)

    def listings(self, lat: float, lon: float, *, reach_km: float) -> list[Listing]:
        found: list[Listing] = []
        for title in self.articles_near(lat, lon, reach_km=reach_km):
            payload = self.get(
                {"action": "parse", "page": title, "prop": "wikitext", "redirects": "1"}
            )
            found.extend(parse_listings(payload.get("parse", {}).get("wikitext", ""), title))
            time.sleep(REQUEST_INTERVAL_S)
        return found


def load(path: Path) -> list[Listing]:
    return [Listing(**row) for row in json.loads(path.read_text())]


def save(listings: list[Listing], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps([asdict(listing) for listing in listings], ensure_ascii=False, indent=1)
    )
