# Travel itinerary planner

Ranked, mapped shortlists of notable places in a city, built from OpenStreetMap.

The spec is in `plans/travel-planner-spec.md`; how it gets built, in independently
shippable rungs, is in `plans/implementation-steps.md`. This repository currently
implements **R0** — the first rung.

## What R0 does

Given a city, produce a ranked list of places worth seeing, on a map. No dates, no
opening hours, no preferences, no routing. Hours arrive at R1.

R0 is a pipeline, not a service. Everything that carries real risk — tuning the tag
whitelist, choosing the rank cut, dedup rules — is offline analysis, and Overpass
forbids per-request querying anyway. So it writes `cities/<slug>.json` and a static
page reads it.

## Usage

```sh
uv sync

uv run travel search "Vancouver"          # find a destination's OSM relation
uv run travel build "Detroit"             # query, enrich, rank, write
uv run travel build "Vancouver" --osm-id 1852574
uv run travel show detroit-michigan       # print a built dataset

python3 -m http.server 8765               # then open web/index.html
```

Pass `--offline` to rebuild from the frozen fixture instead of querying. The
acceptance gate should always run offline: Overpass has no version pinning, so a live
run cannot distinguish an upstream edit from a local regression.

## How selection works

Two independent paths, because neither alone is sufficient.

**Tag predicates** (`src/travel_planner/whitelist.toml`) select museums, galleries,
monuments, artwork, parks, beaches and so on. This file is a first-class asset with a
reason recorded per value — no community-standard list exists to import, and each city
surfaces a different dominant polluter. Paris was 17% trail signage; Lisbon 18% private
palacetes tagged as castles; Detroit 79% non-notable churches.

Selecting is not the inverse of rejecting. The query is an OR-union, so an element
enters on one matching tag and arrives carrying every other tag it has. Implementing
"exclude `historic=yes`" as a reject filter deletes Teatro Nacional Dona Maria II,
which arrived via `amenity=theatre`. There is no reject list.

**The prominence path** selects any named element carrying a Wikidata QID, whatever
its tags, and lets sitelink count decide. Nothing else can reach Michigan Central
Station, Renaissance Center, the Ambassador Bridge or Lions Gate Bridge — OSM
contributors tagged all four as buildings and bridges rather than attractions. Tag
predicates alone find only 19% of Detroit's and 9.3% of Vancouver's QID-bearing
features.

Ranking uses a percentile cut rather than an absolute sitelink threshold, because an
absolute cutoff encodes city fame: Paris's top POI has 189 sitelinks, Lisbon's 57,
Vancouver's 32, Detroit's 29.

## Where the city ends

The query is bounded by **distance from the centre** (25 km), not by the municipal
boundary. What belongs on a city's map is being close enough to reach and worth
reaching, not which council collects the taxes — and an `area()`-restricted query
cannot *return* what sits outside the line, so no downstream filter recovers it.

Under the municipal boundary the pipeline lost Capilano Suspension Bridge and Grouse
Mountain (District of North Vancouver), the Museum of Anthropology and Wreck Beach
(UBC, unincorporated), The Henry Ford — Michigan's most-visited attraction, in
Dearborn — and the city-owned Detroit Zoo, in Royal Oak. Vancouver recall went 9/15 to
14/15 on this change alone.

A radius cannot be tuned per-attraction: for Detroit, 16 km catches Dearborn but not
the Zoo, 18 km catches the Zoo but not Cranbrook, 35 km sweeps in half of Oakland
County. 25 km covered every measured case except Cranbrook at 32 km.

The bound is a **bounding box** in the query and a true circle in the pipeline.
Overpass evaluates `around:` as a real radial search and returned HTTP 504 on every
attempt at this size, while a bbox uses its spatial index; the haversine filter then
trims the corners back.

## Acceptance

`travel recall <slug>` scores a built dataset against a hand-compiled oracle in
`oracles/`, and exits non-zero while any entry is missed. Currently **17/20** for
Detroit and **14/15** for Vancouver.

The oracles are compiled from tourism-board and guidebook sources rather than from the
whitelist's own logic — an oracle derived from the pipeline grades its own homework.
Each entry records the OSM element its compiler verified, so recall is measured by
identity rather than name similarity; name matching is the strict fallback.

## Layout

| Path | What |
| --- | --- |
| `src/travel_planner/sources/overpass.py` | Client. Every guard exists for an observed silent failure |
| `src/travel_planner/sources/geocode.py` | Photon typeahead, Nominatim resolve, ambiguity refusal |
| `src/travel_planner/sources/wikidata.py` | Sitelinks, labels, descriptions, images in one round trip |
| `src/travel_planner/whitelist.toml` | Selection predicates and category precedence |
| `src/travel_planner/pipeline.py` | Dedup, labels, distance filtering, ranking |
| `src/travel_planner/recall.py` | Scores a dataset against its oracle |
| `oracles/` | Hand-compiled recall oracles, with per-entry OSM ids |
| `fixtures/` | Frozen raw responses. Tracked deliberately — they make the gate deterministic |
| `web/index.html` | Static map and list |

## How pins are positioned

Every pin gets a **pole of inaccessibility** — the point inside the feature furthest
from its boundary — rather than a bounding-box centre. The two are not close: Cambie
Bridge is a 1070 × 200 m curved outline whose bbox centre falls 178 m away, outside the
bridge and onto land. `coordinate_source` records which method produced each position.

Getting there takes two passes, because Overpass will not return everything at once:

1. **Main query** — `out tags geom` for ways (real geometry) plus `out tags bb` for
   relations. These must be *separate* `out` statements: `out center geom` is a trap
   where the later mode wins, returning geometry and zero centres.
2. **Relation pass** — plain `out geom` (no `tags` modifier, which suppresses member
   listing) for every relation the pipeline will actually show. About 110–160 KB per
   city.

The rule for the second pass is **if it will be shown, place it properly** — it mirrors
the pipeline's own drop rules rather than applying a bound of its own. An earlier version
reused the destination-size cap as a cost bound, which left Pacific Spirit Regional Park
on the map with a bounding-box centre purely because it was large: at 5.2 km it is no
harder to place than the 3.1 km inlet that was dropped outright.

Relations need three different reductions, and all three occur in real data:

- **Areas** — outer rings are routinely split across several member ways (Coal Harbour is
  four, none closed alone), so members are merged as linework and polygonised rather than
  concatenated, then reduced to a pole of inaccessibility.
- **Routes** — a race circuit has no interior. Detroit's Grand Prix relation carries roles
  like `pit_lane` and `start-finish`; it gets the midpoint of its merged line.
- **Node-only relations** — a station lists platforms and entrances as nodes, which arrive
  with `lat`/`lon` and no `geometry` at all.

With all three handled, **0 of 1,545 Detroit and 0 of 2,190 Vancouver pins** fall back to a
bounding box.

**Feature extent** comes free from `out bb` and does the work distance cannot: it
separates destinations from geography. Burrard Peninsula spans 38 km and Burrard Inlet
31 km, against Granville Island at 0.5 km. The cap applies only to natural features,
because size disqualifies nothing else — Pacific Spirit Regional Park is 5.2 km and
belongs on the map. Note this is a *product* judgement about what counts as a
destination, not a technical limit: with geometry available, a 31 km inlet does have an
honest point, in the middle of the water.

## Known gaps

- **Distance does not count against prominence.** The 25 km radius reaches neighbouring
  municipalities, so their civic buildings now compete on sitelinks with the city's
  actual attractions — Vancouver International Airport (53), New Westminster (42) and
  Port Moody City Hall (31) all outrank Stanley Park (32). Fame alone is not enough;
  how far out something sits has to weigh against it.
- **Features OSM models as infrastructure are unreachable.** Every remaining recall
  miss is one: Stanley Park Seawall is ~50 `highway=cycleway`/`footway` ways with no
  relation and no QID, Detroit RiverWalk ~15 `highway=path` ways, Eastern Market only
  `place=neighbourhood` + `landuse=commercial`. Recovering them needs synthetic
  grouping of same-named ways, which risks inventing entities that should not exist.
- **Universities, office towers and one motor race** rank inside the top 20 (Wayne
  State at 31 sitelinks, the Detroit Grand Prix at 33). Defensible in a list ordered by
  prominence, but whether they belong in a places-to-see list is a judgement R0 does
  not make. That is R2's job.
- **Long linear parks** escape the extent cap because it is scoped to natural features:
  Detroit's Hines Parkway spans 20 km and is categorised as a park.
- **The map has not been visually verified** since the switch to circle layers. The
  list, the data and the gate are all checked; the rendering is not.
