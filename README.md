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
uv run travel worth detroit-michigan      # score what is worth seeing, then cut
uv run travel show detroit-michigan       # print a built dataset
uv run travel show detroit-michigan --shortlist

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

## What is worth seeing

Ranking answers *is this known*. It does not answer *is this worth looking at*, and
the two come apart badly: Vancouver's sitelink order puts BC Place, the airport, a
neighbouring city, two civic buildings and three universities above Stanley Park.
Every one of those is genuinely famous. None is somewhere you go to look at something.

`travel worth` adds the missing judgement, with **no model in the pipeline** — a model
would trade a deterministic, diffable artifact for a per-run API cost and a gate that
cannot tell a prompt change from a data change. Three structured signals replace it.

**Class**, from Wikidata `P31` and the OSM category, splits three cases that a single
keep/drop verdict would collapse:

| Mode | Meaning | Example |
| --- | --- | --- |
| `intrinsic` | The thing itself is the sight | Museum of Anthropology |
| `event` | A container; nothing to see without something scheduled | BC Place, Comerica Park |
| `incidental` | Infrastructure or civic function | Vancouver International Airport |

Only `incidental` means "there is nothing to see". `event` is not a deletion either —
those venues stay in the file, flagged, for R1 to resurrect with a date.

**An event venue earns a place on the shortlist only through its own fabric.** A guide
listing on a venue is evidence that the *programme* is worth attending and says nothing
about whether the building is worth looking at, so it buys the reason line and
confidence but no score. Without that rule every guide-listed venue cleared the cut
automatically — Swangard Stadium at 7.2, listed in the Burnaby article as "home of the
BC High School Championships", next to Surrey Arts Centre, the Shadbolt Centre and
Planet Ant Theater with 0 sitelinks between them.

What survives instead is heritage designation on the building. The rule splits Detroit's
venues exactly right: the Fox, Fillmore, Majestic, Gem and Capitol theatres stay — 1920s
movie palaces, all heritage-listed — while Ford Field, Comerica Park and Little Caesars
Arena drop to 3.3–3.5. Every modern stadium has a named architect, so `P84` alone is not
allowed to carry a concrete bowl over the line. Vancouver went from 15 shortlisted venues
to 2, Detroit from 15 to 6, with no oracle entry lost in either city.

**A travel guide's attention**, from Wikivoyage `{{see}}`/`{{do}}` listings, is the
sharpest signal measured: of 22 Vancouver POIs that rank high and are not sightseeing,
**18 have no listing at all**. It also supplies the actual sentence explaining why to
go, which OSM and Wikidata never do. Wikivoyage's own taxonomy independently confirms
the class split — Detroit's Ford Field, Little Caesars Arena and Comerica Park are all
`do`, never `see`.

Absence is deliberately **not** a penalty. Coverage is ~270 listings against 2,190
Vancouver POIs and the gaps are where no editor happened to write a paragraph, so
presence is strong evidence and silence is weak — the same reason there is no reject
list in the whitelist.

**Wikipedia lead extracts** cover the places no guide wrote up. Wikivoyage reaches
only about a third of the shortlist, and for the rest the fallback was a Wikidata
description — routinely a tautology, since "St. Andrew's Wesley Church" was described
as "church building" and Point Atkinson Lighthouse as "lighthouse". Measured on
shortlisted POIs with no guide text, **90% of Vancouver's and 70% of Detroit's have an
English article**, and the articles carry what the descriptions do not: that
St. Andrew's-Wesley is "renowned for its architecture and art" and holds long-standing
jazz vespers. Reason sources are now 122 / 47 / 5 / 0 for Vancouver and 66 / 60 / 22 / 2
for Detroit across Wikivoyage, Wikipedia, Wikidata and the class fallback.

No sentence is picked heuristically. A Wikipedia lead is definitional by convention, so
the interesting sentence is rarely the first — across a 16-article sample an evaluative
marker appeared in sentence 1 only twice, later 7 times, and not at all 7 times, and
"first sentence with a marker" chose "The bridge forms part of Highways 99 and 1A" for
Lions Gate Bridge. Instead the lead's *opener* is removed, which is a structural edit
rather than a guess because the exact article title is known: "St. Andrew's-Wesley
United Church is an affirming church located in the downtown core" becomes "An affirming
church located in the downtown core". A lead that does not match the pattern exactly is
left intact.

The join is by title through the API's `normalized` and `redirects` arrays, never
positionally — pages come back keyed by pageid in no order, and several inputs collapse
onto one article, so position attaches the wrong prose to the wrong pin silently.

**Formal recognition of the fabric**, from `P1435` heritage designation and the weaker
`P84`/`P149`, separates the Guardian Building from One Wall Centre: both are
`skyscraper` to Wikidata and only one is a landmark. It does not apply to `incidental`
classes on its own — Pacific Central Station is heritage-listed with a named architect
and a named style, and is a working railway station nobody visits. Michigan Central
Station is the same class and genuinely is a sight, and has a guide listing saying so.

**Articles are found by reach, not by name**, for the same reason the Overpass query is.
Fetching only `Vancouver` loses Capilano and Grouse Mountain to `North Vancouver`;
only `Detroit` loses The Henry Ford to `Dearborn`, the Zoo to `Royal Oak` and Cranbrook
to `Bloomfield Hills`. A geosearch grid over the radius recovers all of them.

One source was measured and **rejected**: Commons photo density. Counting geotagged
photographs within 300 m puts BC Place and Rogers Arena at the 500-result cap, level
with Granville Island, while Stanley Park scores 7 — its pin sits deep in the forest.
It measures how built-up somewhere is, not whether anything there is worth a photograph.

### What the UI shows

The list is ordered by worth and filtered to the shortlist, not by sitelinks — the
prominence order is the thing this step exists to correct, and leaving the UI on it
made the whole step invisible. Each row carries the guide's own sentence rather than
the Wikidata description, because "hotel located on West Georgia Street at Burrard
Street" tells a visitor nothing while "bring binoculars for a close-up view of the
building's many gargoyles" is the entire answer.

The popup carries the guide's **full** entry, since a guidebook's opening sentence is
often scene-setting rather than the hook — the Hotel Vancouver's opens on construction
dates and ends on the gargoyles. An `event` POI is tagged "only when something is on"
in the list and says so plainly in the popup, and anything below `verified` shows its
confidence, so a class prior never masquerades as a finding.

The reason line always prefers what is specific to *this* place: the guide's sentence,
then the Wikidata description, and only then the class rationale from `worth.toml`. A
class sentence is a statement about a category read as a statement about a place — it
is identical on every POI of its kind and sits directly above a tag already naming that
category, so it is both vacuous and redundant. All 17 shortlisted Vancouver artworks
read "Made to be looked at."; they now read "sculpture by Douglas Coupland", "sculpture
by Yue Minjun", "lighthouse". Coverage makes it nearly free — of shortlisted POIs with
no guide text, 100% of Vancouver's and 98% of Detroit's carry a description, leaving the
class fallback on 2 POIs across both cities.

Nothing manufactures a reason. When no source has anything to say, the confidence field
reports it, and the UI shows that badge.

### Scoring and cutting are two steps

`worth.toml` holds the class table and the evidence weights, per value with a recorded
reason, exactly like `whitelist.toml`. Scoring attaches a `Worth` to every POI and
**drops nothing**; the cut then marks what clears a threshold. Keeping them apart means
the judgement and the policy are independently reviewable, and a threshold change never
destroys the evidence behind it.

**No prior may reach the cut on its own**, or it stops being a prior. The artwork and
museum priors were 5 against a 4.0 cut, which made them verdicts: every named artwork
was auto-included and Vancouver's shortlist reached 569, three quarters of it scoring
4.0–4.9 on a class prior and nothing else, 291 of them artworks — two were painted
crosswalks. Intrinsic priors now top out at 3 and evidence does the lifting. Event and
incidental weights are not on that scale, because event 1.0 plus a heritage designation
of 3.0 is the deliberate calibration meaning "a designation alone clears the bar".

What separates a public sculpture from a painted crosswalk is that **somebody created a
Wikidata entity for it** — Vancouver has 375 artworks and 18 entities between them. That
flat bonus applies to `intrinsic` only: every stadium has an entity, so for a venue it
says nothing about the building, and applying it there put Comerica Park back over the
cut. It also carries the cost the plan already names — gating on Wikidata presence
raises apparent quality sharply while silently dropping real POIs in under-mapped
cities, where coverage runs 10–15% rather than Vancouver's 15.5%.

The default cut is 4.0, keeping 174 of 2,190 Vancouver POIs and 150 of 1,545 for
Detroit. Every oracle entry survives except Greenfield Village at 2.11, which has no
QID, no guide listing, no heritage record and no Wikipedia article — no signal reaches
it at all.

## Descriptions say what a thing is, not where it is

Wikidata descriptions disambiguate against the whole world, so they name the place
every time: "peninsula in British Columbia, Canada", "stadium at Vancouver, British
Columbia, Canada". In a list where every row is in the same metropolitan area that
suffix appears on every line and distinguishes nothing — and it is the subtitle of
every list item and every map popup. It covered **52% of Vancouver's and 66% of
Detroit's** descriptions; cleaning removes 95% of those.

Only a **trailing** run is removed, and only when every comma-separated segment in it
names this city. Anything less careful breaks real data:

| Description | Result |
| --- | --- |
| `peninsula in British Columbia, Canada` | `peninsula` |
| `museum of Anthropology in Vancouver, British Columbia, Canada` | `museum of Anthropology` |
| `public research university in Greater Vancouver, British Columbia, Canada` | `public research university in Greater Vancouver` |
| `1989 mass shooting in Montréal, Canada` | `1989 mass shooting in Montréal` |
| `indoor arena in Vancouver, Canada, mainly used for ice hockey` | unchanged |
| `suspension bridge that connects Detroit, Michigan, with Windsor, Ontario` | unchanged |

A place name goes only when a connective or a comma introduces it, which is what keeps
the qualifier in "Greater Vancouver" — stripping the alias alone left a dangling
"Greater", and stripping "located in" in one pass left a dangling "located". The
county counts as this place too: nine Detroit parks are described only as being in
"Wayne County, Michigan", behind a `geonames ID = …` bulk-import artifact that is
removed with them.

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
| `src/travel_planner/sources/wikidata.py` | Sitelinks, labels, descriptions, images in one round trip; classes and heritage in another |
| `src/travel_planner/sources/mediawiki.py` | Shared MediaWiki client, retry policy and sentence splitter |
| `src/travel_planner/sources/wikivoyage.py` | Guide listings across the reach — what is worth seeing, and why |
| `src/travel_planner/sources/wikipedia.py` | Lead extracts, for the places no guide wrote up |
| `src/travel_planner/whitelist.toml` | Selection predicates and category precedence |
| `src/travel_planner/worth.toml` | Worth classes and evidence weights, per value with a reason |
| `src/travel_planner/pipeline.py` | Dedup, labels, distance filtering, ranking |
| `src/travel_planner/worth.py` | Worth scoring and the threshold cut |
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

- **Wikivoyage coverage is uneven, and the shortlist inherits it.** Granville Island,
  English Bay Beach and Greenfield Village are all oracle entries with no `{{see}}`
  listing — the guide describes them in running prose instead — so they clear the cut
  on a class prior alone and carry no reason line beyond "what kind of thing this is".
  Parsing article prose, rather than only its listing templates, is the obvious next
  source of recall.
- **The score separates worth from not-worth; it does not rank magnitude.** Evidence
  is additive and sitelinks are capped low on purpose, so a guide-listed neighbourhood
  church with a named architect scores near the Detroit Institute of Arts. The
  heavyweights do stay near the top — DIA is 1st of 344, The Henry Ford 9th, the Zoo
  11th, the Guardian Building 15th — but a visitor's actual priority order is a
  personal-preference problem, which is R2's.
- **`event` is only half an answer without a date.** The step correctly identifies BC
  Place as somewhere with nothing to see unless something is on. It cannot yet say
  whether anything *is* on, which is R1's tiered acquisition and the dynamic-POI work
  in section 3.5 of the plan. Until then an `event` POI is an honest maybe.
- **Heritage designation is a proxy for age, so modern architecture is invisible.**
  Measured across both cities by build era: 51.1% of pre-1950 POIs carry `P1435`,
  6.8% of 1950–1999, and **0 of 34 built since 2000**. Since a designation is what
  lets a venue clear the cut on its fabric, no modern building can. Vancouver
  Convention Centre West has a living roof, LEED Platinum certification, a named
  architect and 400,000 sq ft of public promenade — all of it in the Wikipedia article
  body, none of it in Wikidata (the item carries no `P84` at all) and none in
  Wikivoyage. Awards are not a substitute: `P166` covers 1.2% of entities. The verdict
  survives anyway here, since architect and style together still fall short of the
  bar, but the gap is structural and the next city may not be so lucky.
- **The class table is tuned on two North American cities.** `worth.toml` inherits the
  whitelist's per-city drift problem: Kyoto and Lisbon will surface Wikidata classes
  that neither Vancouver nor Detroit produced. Budget a tuning pass per new test city.
- **Features OSM models as infrastructure are unreachable.** Every remaining recall
  miss is one: Stanley Park Seawall is ~50 `highway=cycleway`/`footway` ways with no
  relation and no QID, Detroit RiverWalk ~15 `highway=path` ways, Eastern Market only
  `place=neighbourhood` + `landuse=commercial`. Recovering them needs synthetic
  grouping of same-named ways, which risks inventing entities that should not exist.
- **Long linear parks** escape the extent cap because it is scoped to natural features:
  Detroit's Hines Parkway spans 20 km and is categorised as a park.
- **The map's own rendering is only spot-checked.** The list, the popup from both the
  list and a map pin, worth ordering and the shortlist filter were verified in a
  browser; clustering behaviour at high zoom and the category legend were not. Note
  that a pin's feature `index` addresses `state.data.pois`, not the visible array —
  numbering the visible one resolved every pin to the wrong POI, and it stayed
  invisible for as long as the two arrays differed only by filtering.
