"""R0's command line.

R0 is a pipeline, not a service. Every deliverable that carries real risk — whitelist
tuning, the ranking cut, dedup rules, the recall oracle — is offline analysis, and
querying Overpass per user request is forbidden anyway (two concurrent slots per IP,
and HTTP 429 after two back-to-back city queries). So the backend has no per-request
work to do until R1 introduces ``resolve_hours(poi, date)``.

Writing ``cities/<slug>.json`` for a static frontend to read defers CORS, an API
schema, a server deployment and keeping Pydantic models in sync with TypeScript
interfaces, and makes every threshold change a reviewable diff.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

from travel_planner.model import CityDataset
from travel_planner.pipeline import (
    DEFAULT_RANK_PERCENTILE,
    MAX_DISTANCE_KM,
    SEARCH_RADIUS_KM,
    build,
    relations_needing_geometry,
)
from travel_planner.recall import load_oracle, measure
from travel_planner.sources import wikipedia, wikivoyage
from travel_planner.sources.geocode import AmbiguousPlaceError, GeocodeError, resolve, suggest
from travel_planner.sources.overpass import (
    OverpassClient,
    OverpassError,
    coalesce,
    relation_geometry_query,
    validate,
)
from travel_planner.sources.wikidata import Facts, fetch_facts
from travel_planner.whitelist import Whitelist
from travel_planner.worth import DEFAULT_CUT, apply_cut, score_dataset

FIXTURES = Path("fixtures")
OUTPUT = Path("cities")


def cmd_search(args: argparse.Namespace) -> int:
    for candidate in suggest(args.query):
        where = ", ".join(filter(None, (candidate["state"], candidate["country"])))
        print(f"  {candidate['name']:<28} {where:<40} {candidate['osm_type']}{candidate['osm_id']}")
    return 0


def cmd_build(args: argparse.Namespace) -> int:
    try:
        city = resolve(args.city, osm_id=args.osm_id)
    except AmbiguousPlaceError as exc:
        print(f"{exc}\n\nRe-run with --osm-id to choose.", file=sys.stderr)
        return 2
    except GeocodeError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    whitelist = Whitelist.load()
    fixture = FIXTURES / f"{city.slug}.json"

    if args.offline:
        if not fixture.exists():
            print(f"no fixture at {fixture}; run without --offline first", file=sys.stderr)
            return 2
        response = validate(json.loads(fixture.read_text()))
    else:
        # The fixture doubles as the cache. Overpass has no version pinning, so an
        # acceptance gate run against the live API cannot tell an upstream edit from a
        # local regression — every gate run should be --offline.
        try:
            with OverpassClient(cache_dir=FIXTURES) as client:
                response = client.query(
                    whitelist.overpass_query(
                        (city.centre_lat, city.centre_lon), int(args.radius_km * 1000)
                    ),
                    cache_key=city.slug,
                )
        except OverpassError as exc:
            print(f"overpass: {exc}", file=sys.stderr)
            return 1

    # Second pass: relations arrive from the main query with only a bounding box, whose
    # centre can sit outside the feature. Fetching their member geometry lets the same
    # pole-of-inaccessibility calculation that ways already use place them properly.
    # Scoped to relations under the extent bound, where it costs 60-90 KB for a city.
    geometry_path = FIXTURES / f"{city.slug}.relations.json"
    relation_geometry: dict[int, list[dict]] = {}

    if geometry_path.exists():
        relation_geometry = {int(k): v for k, v in json.loads(geometry_path.read_text()).items()}
    elif not args.offline:
        ids = relations_needing_geometry(coalesce(response.elements), whitelist)
        if ids:
            try:
                with OverpassClient() as client:
                    detail = client.query(relation_geometry_query(ids))
                relation_geometry = {
                    e["id"]: e["members"] for e in detail.elements if e.get("members")
                }
                geometry_path.write_text(
                    json.dumps({str(k): v for k, v in relation_geometry.items()})
                )
            except OverpassError as exc:
                print(
                    f"relation geometry unavailable ({exc}); using bounding boxes", file=sys.stderr
                )

    dataset = build(
        response,
        city,
        whitelist=whitelist,
        rank_percentile=args.rank_percentile,
        max_distance_km=args.max_distance_km,
        relation_geometry=relation_geometry,
        enrich=not args.no_enrich,
    )

    OUTPUT.mkdir(parents=True, exist_ok=True)
    destination = OUTPUT / f"{city.slug}.json"
    destination.write_text(
        dataset.model_dump_json(indent=1, exclude={"pois": {"__all__": {"tags"}}})
    )

    ranked = dataset.ranked
    print(f"{city.display_name}")
    print(f"  extract      {dataset.generated_from_extract}")
    print(f"  elements     {len(response.elements)}")
    unranked = len(dataset.pois) - len(ranked)
    print(f"  pois         {len(dataset.pois)} ({len(ranked)} ranked, {unranked} unranked)")
    print(f"  written      {destination}")
    return 0


def cmd_worth(args: argparse.Namespace) -> int:
    """Score every POI on whether there is anything to see, then cut.

    Runs against a dataset ``build`` already wrote, rather than inside it: the two
    network sources here are unrelated to discovery, and keeping the step separate
    means a retune re-reads a file instead of re-querying Overpass.
    """
    path = OUTPUT / f"{args.slug}.json"
    if not path.exists():
        print(f"no dataset at {path}; run `travel build` first", file=sys.stderr)
        return 2

    dataset = CityDataset.model_validate_json(path.read_text())
    city = dataset.city

    listings_path = FIXTURES / f"{args.slug}.wikivoyage.json"
    if listings_path.exists():
        listings = wikivoyage.load(listings_path)
    elif args.offline:
        print(f"no fixture at {listings_path}; run without --offline first", file=sys.stderr)
        return 2
    else:
        try:
            with wikivoyage.WikivoyageClient() as client:
                listings = client.listings(
                    city.centre_lat, city.centre_lon, reach_km=args.radius_km
                )
        except wikivoyage.WikivoyageError as exc:
            print(f"wikivoyage: {exc}", file=sys.stderr)
            return 1
        wikivoyage.save(listings, listings_path)

    facts_path = FIXTURES / f"{args.slug}.wikidata-facts.json"
    if facts_path.exists():
        facts = {
            qid: Facts(
                qid=qid,
                classes=frozenset(row["classes"]),
                heritage=row["heritage"],
                architect=row["architect"],
                style=row["style"],
            )
            for qid, row in json.loads(facts_path.read_text()).items()
        }
    elif args.offline:
        facts = {}
        print(f"no fixture at {facts_path}; scoring on OSM categories alone", file=sys.stderr)
    else:
        qids = [p.wikidata for p in dataset.pois if p.wikidata]
        facts = fetch_facts(qids)
        facts_path.write_text(
            json.dumps(
                {
                    qid: {
                        "classes": sorted(f.classes),
                        "heritage": f.heritage,
                        "architect": f.architect,
                        "style": f.style,
                    }
                    for qid, f in facts.items()
                },
                ensure_ascii=False,
                indent=1,
            )
        )

    # Wikipedia is the third prose source and the one that reaches the places no
    # travel guide wrote up — 90% of Vancouver's un-guided shortlist and 70% of
    # Detroit's have an English article, against Wikidata descriptions that are
    # routinely tautologies ("St. Andrew's Wesley Church" -> "church building").
    articles_path = FIXTURES / f"{args.slug}.wikipedia.json"
    articles: dict[str, wikipedia.Article] = {}
    if articles_path.exists():
        articles = wikipedia.load(articles_path)
    elif not args.offline:
        try:
            with wikipedia.WikipediaClient() as client:
                titles = {
                    p.key: wikipedia.english_title(p.wikipedia)
                    for p in dataset.pois
                    if p.wikidata
                }
                unresolved = [
                    p.wikidata for p in dataset.pois if p.wikidata and not titles.get(p.key)
                ]
                by_qid = client.enwiki_titles(unresolved)
                for poi in dataset.pois:
                    if poi.wikidata and not titles.get(poi.key):
                        titles[poi.key] = by_qid.get(poi.wikidata)
                extracts = client.extracts([t for t in titles.values() if t])
                articles = {
                    key: wikipedia.Article(key=key, title=title, extract=extracts[title])
                    for key, title in titles.items()
                    if title and title in extracts
                }
            wikipedia.save(articles, articles_path)
        except wikipedia.WikipediaError as exc:
            print(f"wikipedia unavailable ({exc}); falling back to descriptions", file=sys.stderr)

    score_dataset(dataset.pois, listings=listings, facts=facts, articles=articles)
    kept = apply_cut(dataset.pois, threshold=args.cut)

    path.write_text(dataset.model_dump_json(indent=1, exclude={"pois": {"__all__": {"tags"}}}))

    modes = Counter(p.worth.mode.value for p in dataset.pois if p.worth)
    guided = sum(1 for p in dataset.pois if p.worth and p.worth.guide_kind)
    print(f"{city.display_name}")
    print(f"  listings     {len(listings)} wikivoyage see/do, {guided} matched to a POI")
    print(f"  articles     {len(articles)} wikipedia extracts")
    print("  modes        " + "  ".join(f"{m}={n}" for m, n in modes.most_common()))
    print(f"  cut          {kept}/{len(dataset.pois)} at score >= {args.cut}")
    print(f"  written      {path}")
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    path = OUTPUT / f"{args.slug}.json"
    if not path.exists():
        print(f"no dataset at {path}", file=sys.stderr)
        return 2

    data = json.loads(path.read_text())
    pois = data["pois"]
    scored = any(p.get("worth") for p in pois)

    if args.shortlist:
        pois = [p for p in pois if (p.get("worth") or {}).get("shortlisted")]
    if scored:
        # Once worth exists it is the better order: sitelink rank is the ordering this
        # step exists to correct.
        pois.sort(key=lambda p: -((p.get("worth") or {}).get("score") or 0))

    for poi in pois[: args.limit]:
        rank = poi["sitelinks"] if poi["sitelinks"] is not None else "-"
        head = (
            f"  {rank!s:>4}  {poi['label'][:38]:<38} "
            f"{poi['category']:<11} {poi['distance_from_centre_km']:>5.1f}km"
        )
        if not (worth := poi.get("worth")):
            print(head)
            continue
        print(f"{head}  {worth['score']:>5.1f} {worth['mode']:<10} {worth['confidence']}")
        print(f"        {worth['reason'][:110]}")
    return 0


def cmd_recall(args: argparse.Namespace) -> int:
    """R0's acceptance gate."""
    dataset_path = OUTPUT / f"{args.slug}.json"
    if not dataset_path.exists():
        print(f"no dataset at {dataset_path}; run `travel build` first", file=sys.stderr)
        return 2
    try:
        oracle = load_oracle(args.slug)
    except FileNotFoundError:
        print(f"no oracle at oracles/{args.slug}.json", file=sys.stderr)
        return 2

    result = measure(json.loads(dataset_path.read_text()), oracle)
    total = len(result.matches)
    in_city = len(result.reachable_found)
    print(f"{args.slug}")
    print(f"  recall  {len(result.found)}/{total}  ({in_city} of them inside the city boundary)\n")

    for match in result.found:
        print(f"  \u2713 {match.entry['name'][:50]:<50} {match.found_by}")
    for match in result.missed:
        entry = match.entry
        where = "in city" if entry["inside_city"] else f"OUTSIDE ({entry['municipality']})"
        note = entry["tagging_note"].split("—")[0].split("(verified")[0].strip()[:52]
        print(f"  \u2717 {entry['name'][:50]:<50} {where:<26} {note}")

    # Every oracle entry is now reachable — the query is bounded by distance, not by the
    # municipal line — so any miss is the pipeline's, wherever the place sits.
    return 0 if not result.missed else 1


def main() -> int:
    parser = argparse.ArgumentParser(prog="travel", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    search = sub.add_parser("search", help="find a destination's OSM relation")
    search.add_argument("query")
    search.set_defaults(func=cmd_search)

    build_cmd = sub.add_parser("build", help="build a city dataset")
    build_cmd.add_argument("city")
    build_cmd.add_argument("--osm-id", type=int, help="disambiguate by OSM relation id")
    build_cmd.add_argument("--offline", action="store_true", help="use the frozen fixture")
    build_cmd.add_argument("--no-enrich", action="store_true", help="skip Wikidata")
    build_cmd.add_argument("--rank-percentile", type=float, default=DEFAULT_RANK_PERCENTILE)
    build_cmd.add_argument("--max-distance-km", type=float, default=MAX_DISTANCE_KM)
    build_cmd.add_argument("--radius-km", type=float, default=SEARCH_RADIUS_KM)
    build_cmd.set_defaults(func=cmd_build)

    worth_cmd = sub.add_parser("worth", help="score what is worth seeing, then cut")
    worth_cmd.add_argument("slug")
    worth_cmd.add_argument("--offline", action="store_true", help="use the frozen fixtures")
    worth_cmd.add_argument("--cut", type=float, default=DEFAULT_CUT, help="shortlist threshold")
    worth_cmd.add_argument("--radius-km", type=float, default=SEARCH_RADIUS_KM)
    worth_cmd.set_defaults(func=cmd_worth)

    show = sub.add_parser("show", help="print a built dataset")
    show.add_argument("slug")
    show.add_argument("--limit", type=int, default=25)
    show.add_argument("--shortlist", action="store_true", help="only POIs above the worth cut")
    show.set_defaults(func=cmd_show)

    recall_cmd = sub.add_parser("recall", help="score a dataset against its oracle")
    recall_cmd.add_argument("slug")
    recall_cmd.set_defaults(func=cmd_recall)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
