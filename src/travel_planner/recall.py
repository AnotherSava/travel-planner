"""R0's acceptance gate: does the pipeline find what a visitor would regret missing?

The oracle is compiled independently, from tourism-board and guidebook sources rather
than from the whitelist's own logic — otherwise it grades its own homework. Each entry
records the OSM element the compiler verified, which is what makes recall measurable by
identity instead of by name similarity.

Name matching is the fallback and it is deliberately strict. A substring test scored
"Detroit Zoo" and "Detroit RiverWalk" as found, because both share the token "detroit"
with unrelated POIs — inflating recall from 13/20 to 15/20 in exactly the direction that
flatters the pipeline.
"""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path

ORACLE_DIR = Path("oracles")


def fold(value: str) -> str:
    ascii_form = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", " ", ascii_form.lower()).strip()


# Oracle prose and OSM names abbreviate differently for the same place: the compiler
# wrote "Dr. Sun Yat-Sen", OSM has "Doctor Sun Yat-Sen".
_ABBREVIATIONS = {"dr": "doctor", "st": "saint", "mt": "mount", "ft": "fort"}

# Oracle entries are written as prose ("Gastown & the Steam Clock"); OSM names are not
# ("Gastown Steam Clock"). Requiring every word including articles fails on the join.
_STOPWORDS = {"the", "of", "at", "a", "an", "and", "in", "on", "de", "la"}


def _tokens(value: str) -> set[str]:
    words = (_ABBREVIATIONS.get(t, t) for t in fold(value).split())
    return {w for w in words if w not in _STOPWORDS}


def _components(name: str) -> list[str]:
    """Split a compound oracle entry into separately findable places.

    An entry like "Queen Elizabeth Park & Bloedel Conservatory" names two features, and
    finding either means the pipeline surfaced the place.
    """
    without_gloss = re.split(r"[(—]", name)[0]
    parts = re.split(r"\s*[&/]\s*|\s+and\s+", without_gloss)
    return [p for p in parts if len(_tokens(p)) >= 2]


@dataclass(frozen=True, slots=True)
class Match:
    entry: dict
    found_by: str | None

    @property
    def found(self) -> bool:
        return self.found_by is not None


@dataclass(frozen=True, slots=True)
class Recall:
    matches: list[Match]

    @property
    def found(self) -> list[Match]:
        return [m for m in self.matches if m.found]

    @property
    def missed(self) -> list[Match]:
        return [m for m in self.matches if not m.found]

    @property
    def reachable(self) -> list[Match]:
        """Entries a city-proper query could find at all."""
        return [m for m in self.matches if m.entry["inside_city"]]

    @property
    def reachable_found(self) -> list[Match]:
        return [m for m in self.reachable if m.found]


def measure(dataset: dict, oracle: dict) -> Recall:
    ids = {f"{p['osm_type']}/{p['osm_id']}" for p in dataset["pois"]}
    labels = [(p["label"], _tokens(p["label"])) for p in dataset["pois"]]

    matches = []
    for entry in oracle["entries"]:
        found_by = next((i for i in entry["osm"] if i in ids), None)

        if found_by is None:
            for component in _components(entry["name"]):
                wanted = _tokens(component)
                # Every word of the oracle name must appear in the POI's. Prefix
                # matching in the other direction scored "Stanley Park Seawall" as
                # found because "Stanley Park" is on the map — the seawall is not.
                hit = next((label for label, have in labels if wanted <= have), None)
                if hit:
                    found_by = f"name:{hit}"
                    break

        matches.append(Match(entry=entry, found_by=found_by))

    return Recall(matches=matches)


def load_oracle(slug: str, directory: Path = ORACLE_DIR) -> dict:
    return json.loads((directory / f"{slug}.json").read_text())
