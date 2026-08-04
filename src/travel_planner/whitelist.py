"""Selection predicates, category resolution, and the prominence path.

The distinction this module enforces is the one that matters: selecting is not the
inverse of rejecting. The Overpass query is an OR-union of predicates, so an element
enters the result set on one matching tag and arrives carrying every other tag it
has. A value can therefore be absent from the whitelist and still be present in the
output — 19 Lisbon elements carried ``historic=yes`` despite it never being selected,
having arrived via ``amenity=place_of_worship`` and ``amenity=theatre``, and one of
them was Teatro Nacional Dona Maria II.

So there is no reject list. Unwanted values are never selected. ``reject_when`` and
``reject_name_pattern`` disqualify a *secondary* property of an element a predicate
has already matched, which is a different operation.

The second half of the module is the prominence path, which consults no predicate at
all. Measured on two North American cities, tag predicates reach only 19% and 9.3% of
named QID-bearing features: Michigan Central Station, Renaissance Center, the
Ambassador Bridge and Lions Gate Bridge are tagged as buildings and bridges rather
than attractions, so nothing the whitelist can say will find them.
"""

from __future__ import annotations

import math
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

WHITELIST_PATH = Path(__file__).parent / "whitelist.toml"


def bbox_around(centre: tuple[float, float], radius_m: int) -> tuple[float, float, float, float]:
    """A bounding box enclosing the search radius, as (south, west, north, east).

    The query is bounded by a box rather than by ``around:`` because Overpass evaluates
    a radial filter as a real radial search, while a bbox uses its spatial index. At
    25 km across ~30 clauses the public instance returned HTTP 504 on every attempt at
    the ``around:`` form. The box over-selects at the corners by up to a factor of
    4/pi; the pipeline's haversine distance filter trims those back to a true circle.
    """
    lat, lon = centre
    dlat = radius_m / 111_320
    dlon = radius_m / (111_320 * max(math.cos(math.radians(lat)), 0.01))
    return lat - dlat, lon - dlon, lat + dlat, lon + dlon


@dataclass(frozen=True, slots=True)
class Predicate:
    key: str
    values: frozenset[str]
    category: str

    requires_any_tag: tuple[str, ...] = ()
    """Gate: at least one of these tags must be present."""

    requires_any_value: dict[str, frozenset[str]] = field(default_factory=dict)
    """Gate: at least one of these tag/value pairs must match.

    Satisfied independently of ``requires_any_tag`` — either gate passing is enough,
    because they express alternative kinds of evidence for the same judgement.
    """

    reject_when: dict[str, frozenset[str]] = field(default_factory=dict)
    """Disqualify when *all* of these hold, targeting a specific tag combination."""

    reject_name_pattern: str | None = None
    """Disqualify by name. Used where the discriminating signal is not in the tags."""

    @property
    def gated(self) -> bool:
        return bool(self.requires_any_tag or self.requires_any_value)

    def _passes_gate(self, tags: dict[str, str]) -> bool:
        if not self.gated:
            return True
        if any(tag in tags for tag in self.requires_any_tag):
            return True
        return any(tags.get(k) in v for k, v in self.requires_any_value.items())

    def _rejected(self, tags: dict[str, str]) -> bool:
        if self.reject_when and all(tags.get(k) in v for k, v in self.reject_when.items()):
            return True
        if self.reject_name_pattern:
            return bool(re.search(self.reject_name_pattern, tags.get("name", "")))
        return False

    def matches(self, tags: dict[str, str]) -> bool:
        if tags.get(self.key) not in self.values:
            return False
        if not self._passes_gate(tags):
            return False
        return not self._rejected(tags)


@dataclass(frozen=True, slots=True)
class Prominence:
    """The tag-independent selection path."""

    enabled: bool
    exclude_keys: frozenset[str]
    min_sitelinks: int

    def eligible(self, tags: dict[str, str]) -> bool:
        """Whether an element may be selected on fame alone.

        Excludes keys that are never visitable destinations however famous — highways,
        boundaries, routes — which otherwise arrive with respectable sitelink counts.
        """
        if not self.enabled or "wikidata" not in tags or not tags.get("name"):
            return False
        return not any(key in tags for key in self.exclude_keys)


@dataclass(frozen=True, slots=True)
class Whitelist:
    predicates: tuple[Predicate, ...]
    category_precedence: tuple[str, ...]
    prominence: Prominence
    require_name: bool = True

    @classmethod
    def load(cls, path: Path = WHITELIST_PATH) -> Whitelist:
        raw = tomllib.loads(path.read_text())
        predicates = tuple(
            Predicate(
                key=entry["key"],
                values=frozenset(entry["values"]),
                category=entry["category"],
                requires_any_tag=tuple(entry.get("requires_any_tag", ())),
                requires_any_value={
                    k: frozenset(v) for k, v in entry.get("requires_any_value", {}).items()
                },
                reject_when={k: frozenset(v) for k, v in entry.get("reject_when", {}).items()},
                reject_name_pattern=entry.get("reject_name_pattern"),
            )
            for entry in raw["select"]
        )
        prominence_raw = raw.get("prominence", {})
        return cls(
            predicates=predicates,
            category_precedence=tuple(raw["category_precedence"]),
            prominence=Prominence(
                enabled=prominence_raw.get("enabled", False),
                exclude_keys=frozenset(prominence_raw.get("exclude_keys", ())),
                min_sitelinks=prominence_raw.get("min_sitelinks", 0),
            ),
            require_name=raw.get("require_name", True),
        )

    def matched_categories(self, tags: dict[str, str]) -> list[str]:
        """Every category this element qualifies for, in file order."""
        seen: list[str] = []
        for predicate in self.predicates:
            if predicate.matches(tags) and predicate.category not in seen:
                seen.append(predicate.category)
        return seen

    def category(self, matched: list[str]) -> str | None:
        """Resolve several matches to one displayed category, by declared precedence."""
        ranked = sorted(
            matched,
            key=lambda c: (
                self.category_precedence.index(c)
                if c in self.category_precedence
                else len(self.category_precedence)
            ),
        )
        return ranked[0] if ranked else None

    def accepts(self, tags: dict[str, str]) -> bool:
        if self.require_name and not tags.get("name"):
            return False
        return bool(self.matched_categories(tags))

    def overpass_query(
        self, centre: tuple[float, float], radius_m: int, *, timeout: int = 600
    ) -> str:
        """Build the union, bounded by distance from the city centre.

        Deliberately **not** the municipal boundary. What makes somewhere worth putting
        on a city's map is that it is close enough to reach and worth reaching, not which
        council collects its taxes. An area-restricted query loses Capilano Suspension
        Bridge and Grouse Mountain from Vancouver (District of North Vancouver), the
        Museum of Anthropology and Wreck Beach (UBC, unincorporated), and from Detroit
        both The Henry Ford — the most-visited attraction in Michigan — and the
        city-owned Detroit Zoo, which sits in Royal Oak. No downstream filter can recover
        them, because the query never returns them at all.

        Output is **two separate statements over the same result set**, not one combined
        mode. ``out center geom;`` is a trap — the later mode wins, so it returns
        geometry and no centre at all, silently costing every relation its coordinate.

        Ways need geometry, because a bounding-box centre is not a point inside the
        feature: Vancouver's Cambie Bridge is a 1070 x 200 m curved outline whose bbox
        centre lands 178 m away, off the bridge and onto land.

        Relations get ``bb`` rather than ``center``. It costs the same and yields the
        feature's extent as well as its midpoint — and extent is what separates a museum
        from a 38 km peninsula. Expanding relation members instead would take one city
        from 2.4 MB to 12 MB, almost all of it the giant features that get dropped.

        Each element is therefore emitted twice; ``overpass.coalesce`` merges them.
        """
        south, west, north, east = bbox_around(centre, radius_m)
        body = "\n".join(self._clauses(f"({south:.5f},{west:.5f},{north:.5f},{east:.5f})"))
        return (
            f"[out:json][timeout:{timeout}];\n"
            f"(\n{body}\n)->.result;\n"
            f".result out tags geom;\n"
            f".result out tags bb;\n"
        )

    def _clauses(self, restriction: str) -> list[str]:
        """Every selector in the union, bounded by the given Overpass filter."""
        guard = '["name"]' if self.require_name else ""
        clauses: list[str] = []

        for predicate in self.predicates:
            values = "|".join(sorted(predicate.values))
            selector = f'["{predicate.key}"~"^({values})$"]'

            if predicate.requires_any_tag:
                # Overpass has no OR across tag filters, so each alternative gate
                # becomes its own clause in the union.
                clauses.extend(
                    f'  nwr{selector}["{tag}"]{guard}{restriction};'
                    for tag in predicate.requires_any_tag
                )
            if predicate.requires_any_value:
                for key, allowed in predicate.requires_any_value.items():
                    pattern = "|".join(sorted(allowed))
                    clauses.append(f'  nwr{selector}["{key}"~"^({pattern})$"]{guard}{restriction};')
            if not predicate.gated:
                clauses.append(f"  nwr{selector}{guard}{restriction};")

        if self.prominence.enabled:
            clauses.append("  // prominence path: fame regardless of tagging")
            clauses.append(f'  nwr["wikidata"]{guard}{restriction};')

        return clauses
