"""Wikipedia lead extracts: prose for the places no travel guide wrote up.

Wikivoyage answers "why go" better than anything else available, but it only reaches
about a third of the shortlist — a listing exists where an editor happened to write
one. For the rest the pipeline had nothing but a Wikidata description, and those are
routinely tautologies: St. Andrew's Wesley Church is described as "church building",
Book Tower as "skyscraper", Point Atkinson Lighthouse as "lighthouse". The name
already said that.

Wikipedia closes most of the gap. Measured on the shortlisted POIs with no guide
text, **90% of Vancouver's and 70% of Detroit's have an English article**, and the
articles carry what the descriptions do not — that St. Andrew's-Wesley is "renowned
for its architecture and art" and holds long-standing jazz vespers.

**No sentence is chosen heuristically.** A Wikipedia lead is definitional by
convention, so the interesting sentence is rarely the first: across a 16-article
sample an evaluative marker ("renowned for", "known for", "designated") appeared in
sentence 1 only twice, appeared later 7 times, and was absent 7 times — and picking
"the first sentence with a marker" selected "The bridge forms part of Highways 99 and
1A" for Lions Gate Bridge. So the first sentence goes to the list row and the whole
lead goes to the popup, where the reader can see what the article actually says.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from travel_planner.sources.mediawiki import MediaWikiClient, MediaWikiError, first_sentence

API = "https://en.wikipedia.org/w/api.php"
WIKIDATA_API = "https://www.wikidata.org/w/api.php"

# `prop=extracts` with `exintro` is capped at 20 titles per request; asking for more
# silently returns extracts for the first 20 and bare stubs for the rest.
EXTRACT_BATCH = 20

# `wbgetentities` accepts 50 ids per call.
ENTITY_BATCH = 50

REQUEST_INTERVAL_S = 0.15


class WikipediaError(MediaWikiError):
    """A response that must not be cached or used."""


@dataclass(frozen=True, slots=True)
class Article:
    key: str
    """``osm_type/osm_id`` of the POI this belongs to."""
    title: str
    extract: str

    @property
    def summary(self) -> str:
        """The lead sentence, with its definitional opener removed.

        Wikipedia's house style opens "<Title> is a <definition>", which restates the
        name the reader just read — the same tautology the Wikidata descriptions had.
        Because the exact title is known this is a structural edit rather than a guess:
        "St. Andrew's-Wesley United Church is an affirming church located in the
        downtown core" becomes "An affirming church located in the downtown core".

        Anything that does not match the pattern exactly is left alone, so a lead like
        "Michigan Central Station (MCS, also known as ...) is the historic ..." keeps
        its parenthetical rather than being mangled.
        """
        sentence = first_sentence(self.extract)
        opener = re.compile(
            rf"^(?:The\s+)?{re.escape(self.title)}\s+(?:is|was)\s+", re.IGNORECASE
        )
        if (match := opener.match(sentence)) is None:
            return sentence
        rest = sentence[match.end() :].strip()
        return rest[:1].upper() + rest[1:] if rest else sentence




def english_title(wikipedia_tag: str | None) -> str | None:
    """The English title from an OSM ``wikipedia`` tag, if it names one.

    The tag is ``lang:Title`` and the prefixes are mixed — a Lisbon pull carried 183
    ``pt``, 46 ``en``, 4 ``de`` and 1 ``fr``. Only the English ones are usable here;
    everything else has to go through the QID's sitelink instead.
    """
    if not wikipedia_tag:
        return None
    language, separator, title = wikipedia_tag.partition(":")
    return title.strip() if separator and language.strip().lower() == "en" else None


class WikipediaClient(MediaWikiClient):
    endpoint = API
    error_type = WikipediaError

    def enwiki_titles(self, qids: list[str]) -> dict[str, str]:
        """QID to English article title, for POIs whose OSM tag names another language."""
        found: dict[str, str] = {}
        wanted = sorted(set(qids))
        for start in range(0, len(wanted), ENTITY_BATCH):
            batch = wanted[start : start + ENTITY_BATCH]
            payload = self.get(
                {
                    "action": "wbgetentities",
                    "props": "sitelinks",
                    "sitefilter": "enwiki",
                    "ids": "|".join(batch),
                },
                url=WIKIDATA_API,
            )
            for qid, entity in payload.get("entities", {}).items():
                if title := entity.get("sitelinks", {}).get("enwiki", {}).get("title"):
                    found[qid] = title
            time.sleep(REQUEST_INTERVAL_S)
        return found

    def extracts(self, titles: list[str]) -> dict[str, str]:
        """Lead extracts, keyed by the title that was *asked for*.

        The join is the sharp edge here. The API returns pages keyed by pageid in no
        particular order, and it rewrites titles on the way: ``normalized`` records
        capitalisation and underscore fixes, ``redirects`` records where a title
        actually resolved to. A 20-title request routinely comes back with fewer
        pages, because several inputs collapsed onto one article. Matching by position
        therefore attaches the wrong prose to the wrong pin, silently.
        """
        found: dict[str, str] = {}
        wanted = sorted({t for t in titles if t})

        for start in range(0, len(wanted), EXTRACT_BATCH):
            batch = wanted[start : start + EXTRACT_BATCH]
            payload = self.get(
                {
                    "action": "query",
                    "prop": "extracts",
                    "exintro": 1,
                    "explaintext": 1,
                    "redirects": 1,
                    "titles": "|".join(batch),
                },
            )
            query = payload.get("query", {})
            resolved = resolve_titles(batch, query)

            by_title = {
                page["title"]: page.get("extract", "")
                for page in query.get("pages", [])
                if not page.get("missing")
            }
            for asked, final in resolved.items():
                if extract := by_title.get(final, "").strip():
                    found[asked] = extract
            time.sleep(REQUEST_INTERVAL_S)

        return found


def resolve_titles(asked: list[str], query: dict) -> dict[str, str]:
    """Map each requested title to the title its page actually came back under.

    This is the join the whole module turns on. The API returns pages keyed by pageid
    in no meaningful order and rewrites titles on the way: ``normalized`` records
    capitalisation and underscore fixes, ``redirects`` records where a title actually
    resolved to. Several inputs routinely collapse onto one article, so a 20-title
    request comes back with fewer pages — a Lisbon probe returned 19 pages for 20
    titles. Matching by position therefore attaches the wrong prose to the wrong pin,
    and does it silently.

    The two tables are applied in order because a redirect's ``from`` is the
    *normalised* title, not the raw one.
    """
    resolved = {title: title for title in asked}
    for table in ("normalized", "redirects"):
        hops = {hop["from"]: hop["to"] for hop in query.get(table, []) if "from" in hop}
        for key, current in resolved.items():
            if current in hops:
                resolved[key] = hops[current]
    return resolved


def load(path: Path) -> dict[str, Article]:
    return {row["key"]: Article(**row) for row in json.loads(path.read_text())}


def save(articles: dict[str, Article], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            [asdict(a) for a in sorted(articles.values(), key=lambda a: a.key)],
            ensure_ascii=False,
            indent=1,
        )
    )
