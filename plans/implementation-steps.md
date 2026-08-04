# Travel Itinerary Planner — Implementation Plan

Companion to `travel-planner-spec.md`. The spec describes the finished system; this
document describes how to get there.

---

## 1. Approach

The spec's Goal section defines success as "a complete, timed route through the city."
Building toward that as a single deliverable means nothing is usable until the last
stage works, and the riskiest data problem in the project sits underneath everything
else.

This plan treats the spec's stages as a **ladder of independently valuable releases**
instead. Each rung ships something a person can use, and each one consumes the rung
below it. The complete timed route is the last rung, not the definition of done.

Two consequences worth stating up front:

- **A rung failing does not fail the project.** If scenic routing turns out to be
  undeliverable, rungs 0–4 are unaffected. If the timed route proves intractable, a
  ranked, hours-resolved, geographically grouped shortlist is still a product.
- **Measurement moves inside the releases.** An earlier draft of this plan opened with
  throwaway spikes to falsify the risky assumptions. Those are gone: the opening-hours
  accuracy study is now R1's acceptance test, and the POI source comparison is R0's.
  Nothing is built to be discarded.

### Delivery surface

Web UI — platform-independent, and the plan is a spatial object whose bugs (bad
geocodes, a candidate 40 km outside the city, a day that crisscrosses town three times)
are invisible in a text list and obvious on a map. The map earns its place as a
debugging instrument before it is a product surface.

Backend in Python (OR-Tools matters from R4 onward, and the solver is touched by five
of eight rungs), frontend in TypeScript. Verify the Python `opening_hours` parser
handles public-holiday and month selectors before committing to it — that is the part
naive ports skip, and it is most of R1's value.

**R0 is a command-line pipeline, not a service.** Every risky R0 deliverable is offline
analysis — whitelist tuning, ranking cutoffs, dedup rules, the recall oracle — and the
plan already forbids per-request source queries. So R0's backend has no per-request work
to do. Ship it as a Python CLI that writes `cities/<slug>.json`, consumed by a static
TypeScript frontend. That defers CORS, the API schema, server deployment, and keeping
Pydantic models in sync with TypeScript interfaces across a dozen POI fields, and it
makes every threshold change a reviewable diff. Introduce the web service at R1, where
`resolve_hours(poi, date)` gives the backend a genuine per-request job.

---

## 2. Release ladder

| Rung | Ships | Size |
| --- | --- | --- |
| **R0** | Ranked, mapped shortlist of notable places in a city | L |
| **R1** | Opening hours resolved to the user's specific dates | L |
| **R2** | Personal preference scoring, exterior/interior worth, dwell estimates | M |
| **R3** | Geographic grouping into half-day and full-day clusters | S |
| **R4** | The timed route — the spec's stated Goal | XL |
| **R5** | Scenic leg selection | M |
| **R6** | Interactive re-plan under user constraints | M |
| **R7** | Public transport mode | L |

R0 and R1 are detailed in sections 4 and 5. The rest are summarized in section 6.

**R0 is a step, not a release.** Its user-facing value is modest — it competes with
existing "top sights" listings. Its real value is internal: it settles the POI source
by measurement, produces the recall oracle every later rung is judged against, and
yields the hours-coverage number that sizes R1. Judge it on whether it de-risked the
next rung, not on enthusiasm.

**R1 is the first thing worth showing a stranger**, because "which of these places are
actually open on the days I am there, holidays included" does not exist anywhere else.

---

## 3. Cross-cutting design decisions

These cost nothing to adopt now and are data-model migrations later. Settle them before
writing R0.

### 3.1 The schedulable atom is an occurrence, not a POI

A POI *generates* occurrences; an event *is* one. An occurrence is a place, a time
window, a dwell, and a value.

This single choice does four jobs:

- A POI yields an **exterior** occurrence (daylight window, ~10–15 min, no opening-hours
  constraint) and an **interior** occurrence (inside opening hours, real dwell). The
  solver picks between them.
- Static opening hours resolve into occurrences.
- Events (a concert, a temporary exhibition) are occurrences with a hard-pinned window.
- Meals, breaks, and slack are occurrences with soft windows.

Model "POI with opening hours" as the atom instead, and events, meal breaks, and the
exterior/interior split all become retrofits through every layer.

### 3.2 Enrichment is staged by funnel position

Every expensive layer in this system follows the same shape: something cheap across all
candidates, something expensive across the few that survive.

| Layer | Broad and cheap | Narrow and expensive |
| --- | --- | --- |
| Hours | Whatever the bulk source carries | Per-POI website check for the shortlist |
| Scoring | Category priors, prominence | Model scoring on the ranked subset |
| Travel time | Straight-line estimate | Real routing on chosen legs |
| Scenic | — | K−1 chosen legs only |

Build **one** progressive-enrichment mechanism rather than four bespoke ones, and make
every resolver lazy and per-item (`resolve_hours(poi, date)`, cached) rather than a batch
pass over the full candidate list. Getting this wrong at R1 means unwinding a cost model
at every later rung.

### 3.3 Confidence is a first-class value

Every derived fact carries `verified | inferred | assumed | unknown`. Three rules:

- **"Closed" and "unknown" must never render identically.** One is a fact, the other is
  ignorance, and conflating them is what makes the product untrustworthy.
- Confidence reaches the solver, not just the display layer — a confidently-open stop
  should beat a marginally better uncertain one.
- Confidence legitimately rises as a POI advances through the funnel. R1's output is
  less certain than R4's, and telling the user "we will check these properly once you
  pick" is honest.

### 3.4 Interfaces to get right on first write

- `travel_time(a, b, mode, depart_at)` — the walking implementation ignores `depart_at`,
  but public transport in R7 does not, and retrofitting it means rewriting the solver.
- An itinerary is an **alternating sequence of stop and transfer segments**, not a list
  of stops. Meals, slack, scenic detours, and transit waits all need somewhere to live
  that is not a stop.
- The solver is **multi-tour with an optional depot** from its first version, so "one
  continuous route" is the depot-free special case of "one route per day." This turns
  the multi-day open question from an architecture fork into a parameter.
- **Zoned datetimes everywhere, from the first commit.** Hours are local, dates are
  local, and a trip window can cross a DST boundary. A naive UTC comparison produces
  off-by-hours errors that read as data problems for weeks.

### 3.5 Static and dynamic POIs

| | Static (zoo, cathedral, bar) | Dynamic (exhibition, concert, fireworks) |
| --- | --- | --- |
| Time | Recurring weekly rule + exceptions | One concrete interval, once |
| Source | OSM / Places / Wikidata — mapped, stable | Venue sites, ticketing, city calendars |
| Scoring | Exterior vs interior worth | "Is this a big deal", "are tickets needed" |
| Scheduling | Wide window, solver has freedom | A hard pin the day bends around |
| Freshness | Stable for years | Stale in weeks; often unpublished months ahead |

**Dynamic POIs have no R0 form.** A static place can be listed generically and resolved
to dates later; an event either falls inside the trip window or does not exist for the
user. So dynamic work cannot start before R1.

Bound the scope when it does start, in this order: temporary exhibitions and special
openings *at POIs already in the catalogue* (nearly free once R1's website verification
exists, and reuses POI identity); then venue-based events; then city-wide happenings.
Note that tickets change the failure mode — a sold-out concert the evening was built
around costs more than a closed museum.

### 3.6 Exterior visits are a planned alternative, not error recovery

The exterior occurrence exists for the case where the trip window genuinely cannot hit
opening hours: a 19:00 arrival, a Monday, a four-hour layover. That is a good outcome
from *correct* data.

It is **not** a licence to tolerate bad hours data. Wrong hours do not merely cost one
missed entry — the whole day was shaped and ordered around a stop that was never
available, and the alternatives that were available got dropped. A large share of POIs
also have no exterior value at all, so it was never a general safety net.

Consequence for R4's objective function: **do not let the solver treat the two as freely
interchangeable by value density.** A 15-minute facade scoring 6 beats a 90-minute
interior scoring 8 on value-per-minute every time, so a naive objective fills the day
with facades and enters nothing. The interior wins whenever it is actually schedulable;
the exterior is chosen because the interior is unavailable or genuinely weak. Closer to
a ranked preference than a scalar tradeoff.

---

## 4. R0 — Ranked, mapped shortlist

### Goal

Answer one question: *what is worth seeing in this city, and where is it?*

### What the user sees

A destination box. Type "Lisbon", get a map with ranked pins and a list beside it: name,
category, one line of description, a link to Wikipedia or the official site, an image
where one exists.

### Deliverables

| # | Deliverable | Notes |
| --- | --- | --- |
| 0.1 | Overpass client with **silent-failure detection**, pinned endpoint, per-city cache | Failures return HTTP 200 — see build order |
| 0.2 | **Git-tracked raw response fixture per test city** | Makes the recall gate deterministic |
| 0.3 | Coordinate normalisation at ingest, with representative points for closed ways | 67% of elements carry no top-level coordinate |
| 0.4 | **City-boundary decision** + `distance_from_centre_km` on every POI | Defines what the recall oracle can contain |
| 0.5 | Geocoder: Photon for the picker, Nominatim for the authoritative resolve | Store the whole address object, not a scalar |
| 0.6 | **Tag selection predicates**, versioned with per-value rationale | Never a reject list — see build order |
| 0.7 | **Ordered category precedence table** | 64 Lisbon elements match 2–3 families |
| 0.8 | Wikidata round trip: sitelinks, `rdfs:label@en`, `schema:description`, `P18` | One SPARQL call, `P625` gate inline |
| 0.9 | **Latin-script label chain** | 65.7% of central Kyoto has no Latin label from any tag |
| 0.10 | Wikipedia extract and thumbnail enrichment, joined by title | OSM supplies almost no descriptions or images |
| 0.11 | Dedup: name+coordinate primary, QID corroborating | Priority is the reverse of the obvious one |
| 0.12 | Rank/percentile ranking + explicit policy for unranked POIs | Absolute cutoffs are a city-fame proxy |
| 0.13 | Map + ranked list UI, with a real tile provider and attribution | Tile source is a licensing decision |
| 0.14 | **Hand-written top-25 per test city** | Permanent recall oracle; reused at R2 |
| 0.15 | **Hours-coverage report** | The number that sizes R1 |

### Build order

Ordered around what fails *silently*, not around what is interesting.

**1. A trustworthy client, before any whitelist work.** Overpass does not fail loudly.
A timed-out query returns **HTTP 200, valid JSON, `"elements": []`**, with the only
evidence in a `remark` key. A partially completed query returns *partial* elements plus
the same remark — plausible-looking data. An area id derived from a node returns HTTP
200 with zero results and no error at all. Observed failures included HTTP 504 with an
XHTML body, which breaks JSON parsing rather than tripping a status check.

Mirrors make it worse: the main endpoint served data current to the minute while a
mirror served data 5–9 weeks stale, **non-deterministically across calls minutes apart**.

Combined with the per-city caching this plan mandates, any of these poisons a cache
entry permanently, and the day is then spent debugging a whitelist for an upstream
problem. So, first:

- Reject on `remark` present, on `elements == []`, and on `osm3s.timestamp_osm_base`
  older than ~7 days.
- Never write a rejected response to cache.
- Pin one endpoint rather than failing over.
- Freeze each test city's raw response as a git-tracked fixture, and run the acceptance
  gate against the fixture. Overpass has no version pinning, so a live-API gate cannot
  distinguish an OSM edit from a whitelist regression.

Same day: **normalise coordinates at ingest.** In a Lisbon pull of 1,032 elements, 548
were ways and 141 relations — **67% carry no top-level `lat`/`lon`** unless `out center`
is requested, and that 67% is where the castles, monasteries, parks and monuments live.
Two follow-on traps: `out center geom` returns geometry and *no* centre (the later mode
wins, so adding `geom` for debugging silently drops every coordinate), and `out center`
returns the **bounding-box centre, not a centroid** — point-in-polygon on 316 closed
named Lisbon ways put **25 of them (7.9%) outside their own polygon**, including Palácio
Fronteira and three parks. Compute a pole-of-inaccessibility or centroid for closed ways
instead. On a product whose pitch is "and where is it", 8% of pins visibly off-feature
is noticed in the first minute and is invisible in the JSON.

**2. Decide what "the city" means — it defines the oracle rather than following from it.**
Nominatim returns **two results both displaying exactly `Lisboa, Portugal`**: an 11.7 ×
12.4 km city relation and a 71.5 × 62.0 km one, roughly thirty times the area. Naples
repeats the pattern. Running the curated whitelist against Lisbon city proper returns
**zero hits for Sintra, Pena, Cascais, Estoril, Queluz and Cristo Rei** — and Pena Palace
is arguably the single most-photographed thing people do on a Lisbon trip.

If the hand-written top-25 contains Pena, then city-proper is a *failing implementation*,
not a scoping choice. Cheapest honest answer: query the admin relation, store
`distance_from_centre_km` on every POI, and add a second wider pull banded as day trips —
the UI can separate them without needing a transport mode.

Gate the geocoder on `addresstype in {city, town, municipality}`, reject states,
countries and regions with a message, and reject non-relation results outright rather
than deriving an area id from a node. Use **Photon** for the typeahead — it is the
OSM-based geocoder built for exactly the case Nominatim's policy forbids — and Nominatim
for the authoritative resolve.

**3. Selection predicates and a separate category table.** A naive tag pull is unusable:
Paris returns 15,790 objects that are 17% trail signage, 16% private residential gardens
and 13% hotels, with museums and attractions together around 3%. No community-standard
list exists to import — the OSM wiki explicitly declines to define one — so this is
hand-curated and owned.

Starting point, measured to cut Paris from 15,790 to 2,406 while tripling every quality
metric:

- `tourism` in `museum, gallery, attraction, viewpoint, zoo, theme_park, aquarium`
- `historic` in `castle, monument, city_gate, citywalls, fort, palace, manor,
  monastery, archaeological_site, tower`
- `amenity` in `place_of_worship, theatre, arts_centre`
- `leisure` in `park, garden` — gated on name + (wikidata or wikipedia)
- `name` required on everything

Three corrections that Paris tuning alone could not surface:

- **Secondary discriminators are needed before thresholds.** In Lisbon, `historic=castle`
  is 185 of 1,032 elements — 18% of the list, in a city with one castle. Nearly all are
  `castle_type=palace|stately` on `building=house`: private townhouses you cannot enter.
  Likewise `tourism=attraction` decomposes into 41 fountains, 17 ponds, **11 individual
  street trees** and 5 bare rocks. Adding `castle_type` and `natural` as discriminators
  removes roughly 250 of 1,032 rows on their own.
- **The whitelist is a per-city artifact.** Each new city surfaces a different dominant
  polluter. Budget a tuning pass per test city rather than one up-front pass.
- **Never implement exclusions as a reject filter.** The whitelist is an OR-union of
  selection predicates, so an element enters on one good tag and drags its other tags in
  with it. Nineteen Lisbon elements carry `historic=yes` despite it being excluded —
  they arrived via `place_of_worship` and `theatre`, and one of them is **Teatro Nacional
  Dona Maria II**, a genuine top-25 POI. Rejecting on presence deletes it, and the recall
  oracle only counts what is missing *after* you already believe the filter is right.

  Keep two separate structures: a **selection** predicate list, never a reject list, and
  an **ordered category precedence table** for display. Sixty-four Lisbon elements match
  two or three families at once (Torre de Belém is `attraction` + `fort`), so the
  category column the UI promises has no defined derivation without one.

**4. Enrichment and ranking, in one SPARQL round trip.** Pull sitelink counts,
`rdfs:label@en`, `schema:description` and `P18` together, with the `wdt:P625` gate inline
so concept-mistags are filtered in the same call. Measured: 484 QIDs in 1.1 s.

Two things the earlier draft got wrong:

- **Absolute sitelink cutoffs are a proxy for city fame, not POI quality.** Lisbon's
  top-ranked POI has 57 sitelinks against the Eiffel Tower's 189. Use a rank or
  percentile cut so one constant works for both.
- **Write down what rank the unranked get.** Fifty-one percent of Lisbon elements have
  no Wikidata tag at all. Score them zero and half the city is invisible; score them
  anything else and they flood the list. Most defensible is a separate "unranked, locally
  mapped" tier the UI can reveal on demand.

**5. Readability is a second data pipeline, not polish.** OSM supplies almost none of it:
Lisbon has `image` on 0.3% of elements, `website` on 19.4%, `description` on 41.1% — and
those descriptions are Portuguese, median 243 characters, with at least one being
free-text state (`Encerrado temporrariamente`) that contradicts the confidence model.

So descriptions and images come from Wikipedia and Wikidata. Two sharp edges:

- The `wikipedia` tag is `lang:Title` with **mixed prefixes** (183 pt, 46 en, 4 de, 1 fr
  in Lisbon), so batch per wiki host. English needs a QID → enwiki sitelink, which the
  51% without a QID do not have.
- The MediaWiki API returns a **dict keyed by pageid, unordered**, and redirects and
  normalisation collapse titles — 20 input titles returned 19 pages. **Join by title
  through the `normalized` and `redirects` arrays, never positionally**, or descriptions
  land on the wrong pins.

Same step: **the Latin-script label chain**, which is a harder Kyoto gate than any
threshold. In central Kyoto only **3.7% of `name` values are pure ASCII**, `name:en`
covers 31.0%, romaji 2.2%, and **65.7% have no Latin-script label available from any
tag**. Lisbon is quietly the same shape at 7.4% `name:en`. An English-speaking user
planning Kyoto otherwise gets a list they cannot read, search, or match to a guidebook.
Chain `name:en` → Wikidata `rdfs:label@en` → `int_name` → romaji → `name`, pull the label
in the SPARQL call already being made, and always keep the local name alongside — that is
what the signage says.

**6. Dedup, with the priority reversed.** In Lisbon only 13 QIDs sit on more than one
element (2.9% of the pull); the Paris case of one QID on 37 metro entrances is a Paris
artifact. Meanwhile **30 names repeat across 65 elements, with 22 same-name pairs within
150 m**, and most have no QID at all. So name+coordinate does nearly all the work and QID
is the corroborating signal, not the primary path.

QID merging is also unsafe on its own: one Lisbon QID spans a cable-car line and both its
stations, so merge-by-first order can label the pin "Norte"; another spans a viewpoint and
its adjacent garden; a third is the *species* Tipuana tipu sitting on three separate street
trees. When merging, pick the surviving name by rule — longest, or the one matching the
Wikidata label — never by iteration order.

**7. The map, with the tile decision made deliberately.** The OSMF tile policy requires a
unique app-identifying User-Agent, forbids library defaults, requires a valid `Referer`,
and **explicitly forbids a restrictive `Referrer-Policy` that suppresses it** — which is
exactly what the security-header presets most hosts ship (`no-referrer`,
`strict-origin-when-cross-origin`) do. Deploying with defaults violates the policy
silently. It also requires honouring cache headers or caching tiles ≥7 days, forbids
pre-seeding and tile archives, and permits only interactive human viewing. Every Leaflet
tutorial hardcodes that host.

Pick a real provider in week one — MapTiler's free tier, Stadia, or self-hosted Protomaps
PMTiles (one archive per city on object storage, which fits this product's per-city shape
unusually well) — and put the attribution block in the same commit. Note that ODbL
attaches to the POI *data* as well as the tiles, Wikipedia extracts are CC BY-SA, and
Commons thumbnails carry per-image licences. Revisit if R1 buys Google Places for hours,
since its terms require display on a Google map.

### Acceptance criteria

Run against the frozen fixtures, not the live API, so a threshold change and an upstream
edit are distinguishable.

- **Recall:** finds at least 20 of the hand-written top-25, for each test city.
- **No duplicates:** the same cathedral does not appear as three pins.
- **Coordinates on-feature:** no pin outside its own polygon.
- **Readable labels:** every POI has a Latin-script label, or is explicitly marked as
  having none.
- **Readability:** the list is scannable without scrolling past street furniture.
- **A non-European city passes.** Thresholds tuned on Paris produce a near-empty map for
  Kyoto — this is a release gate, not a caveat.

### Out of scope

Dates, opening hours, transport mode, preferences, personal scoring, routing, timing,
cross-source POI identity, and any HTTP service.

### Notes and risks

- **Hours are deliberately absent from R0.** Measured coverage on the curated whitelist
  is 18% (Paris), 12% (Kyoto), 10% (Mexico City); on the 50 most famous Paris POIs, only
  25 carry hours at all. Displaying a field that is empty on 80–90% of pins reads as
  broken. Hours start at R1, from near zero.
- **Wikidata is enrichment, never identity.** Coverage on the named sightseeing set is
  38–40% in Paris and Prague but 10–15% in Kyoto and Mexico City. Keep a
  name+coordinate fallback and build no feature that assumes a QID exists.
- **Full cross-source identity is premature here.** The `wikidata=` join, provenance,
  and merge resolution earn their keep when *merging* sources. With a single adapter,
  dedup is trivial. Defer the machinery to whenever a second source arrives.
- **Public Overpass allows 2 concurrent slots per IP**, and returned HTTP 429 after two
  back-to-back city queries during testing. Cache by city from the first commit; never
  query per user request. Do not self-host — revisit only past ~10k requests/day.
- **Wikidata Query Service is flaky infrastructure**, and refused requests with a
  1-per-minute rule during an outage mid-investigation. Retry, back off, and keep a
  stale-cache fallback.
- **The sitelink signal ranks the subject, not the object.** A statue of a famous person
  inherits the person's fame. The `P625` gate mitigates this only partly.
- Gating weak categories on Wikidata presence roughly triples apparent quality while
  silently dropping real POIs in under-mapped cities. That is a product decision, not a
  tuning constant.

---

## 5. R1 — Hours resolved to the user's dates

### Goal

For each place, say what it does on **the specific days this person is there** —
including public holidays, seasonal closures, and last admission — and be honest about
how confident that answer is.

### What the user sees

The R0 list plus dates, with a per-day strip on each entry:

> **Mon 14** — closed (public holiday: Assunção de Nossa Senhora)
> **Tue 15** — 10:00–18:00, last entry 17:00
> **Wed 16** — 10:00–18:00

Plus a confidence marker per entry and a filter for "open on at least one of my days."

### Deliverables

| # | Deliverable | Notes |
| --- | --- | --- |
| 1.1 | Category class table: `always_open` vs `hours_apply` | Free coverage win — do first |
| 1.2 | `opening_hours` parser integration | Real grammar; never regex |
| 1.3 | Public-holiday calendar keyed on country **and subdivision** | Regional holidays close things |
| 1.4 | Last-admission priors by category | Applied as a window shrink, labelled `assumed` |
| 1.5 | Staleness signal from `check_date` | Distinguishes fresh from rotten inference |
| 1.6 | `resolve_hours(poi, date)` — lazy, per-POI, cached | The core of the release |
| 1.7 | Tiered acquisition pipeline (below) | |
| 1.8 | Per-day UI strip, confidence rendering, "open on my days" filter | |
| 1.9 | **False-open measurement report** | The release gate |

### The resolver

```
resolve_hours(poi, date) -> {
  status:     open | closed | always_open | unknown,
  intervals:  [(09:00, 17:30), ...],          # local, zoned
  confidence: verified | inferred | assumed,
  reason:     "PH: Assunção de Nossa Senhora" | "Apr-Oct season" | "category default",
  source, checked_at
}
```

### Build order

1. **Split the no-data bucket by category — before touching a parser.** Absence of
   hours does not mean the same thing everywhere. A museum with no `opening_hours` is
   genuinely unknown; a bridge, plaza, viewpoint, public park, avenue, or monument in a
   square is *always accessible*. Half the missing hours among famous Paris POIs are
   open public spaces — Champs-Élysées, Place de la Concorde, Champ de Mars — not
   unmapped data. A lookup table converts those into a confident answer for free and
   stops the UI reading as broken.

2. **Parser and holiday calendar.** This yields `inferred` for everything carrying a
   rule. The `opening_hours` syntax is a genuine grammar (`Mo-Fr 10:00-18:00; Su off;
   PH off`, month selectors, `sunrise-sunset`) — use a real parser.

   Holidays need **municipality granularity in at least some countries**, not just
   country and subdivision. Nominatim populates `address.state` for US, Italian and
   British results but leaves it empty for Portuguese sub-city ones — and Portugal's
   relevant closure driver is the *municipal* holiday: Lisbon's Santo António on 13 June,
   precisely the patron-saint case that closes things the national calendar has never
   heard of. This is why R0's deliverable 0.5 stores the whole address object plus
   `osm_type`, `osm_id` and `extent` rather than a scalar subdivision string;
   re-geocoding every cached city later to recover a discarded field is a self-inflicted
   migration.

3. **Measure, then decide how much more to build.** Sample 30 POIs per city against a
   date six weeks out, hand-check each against its official website, and report three
   numbers *separately*:

   - % correct
   - % unknown
   - **% false-open** — claimed open, actually shut

   The third is the release gate. Unknown is recoverable; wrong sends someone across
   town to a locked door. This number determines whether the verification tier below is
   optional or mandatory before shipping.

4. **Tiered acquisition.** OSM carries hours for only 10–18% of a curated list, so
   per-POI enrichment is not a refinement step — it is the primary mechanism, and the
   funnel is what makes it affordable at all. You cannot fetch 2,400 websites; you can
   fetch 25.

   | Tier | Source | Scope | Cost |
   | --- | --- | --- | --- |
   | 0 | OSM tags + category class | All candidates | Free |
   | 1 | Commercial source | Ranked shortlist | Metered, batchable |
   | 2 | Official website fetch + extraction | POIs being scheduled | Per-POI, expensive |
   | 3 | Re-check at T−7 days | Final plan only | Per-POI |

   Tier 1 is where a commercial source earns its cost, since its hours coverage far
   exceeds OSM's. The natural shape is a hybrid: **OSM for discovery, commercial and
   official sites for hours.**

5. **Verify the shortlist, not the selection.** Selection depends on hours and hours
   quality depends on selection, which is a loop: verify after choosing, discover a stop
   is shut, re-select, and pull in an alternative that was never verified either. Avoid
   it by running tier 2 over the chosen set *plus* a few ranked alternates, then
   selecting from verified data. A few wasted checks, no feedback edge.

### Acceptance criteria

- False-open rate low enough to act on, measured by hand against official sites on a
  held-out sample.
- `always_open` correctly classified for open public spaces — no "unknown" on the
  Champs-Élysées.
- "Closed" and "unknown" visibly distinct in the UI.
- A public holiday inside the trip window is caught and named.
- Multi-day windows render per-day without special handling.

### Out of scope

Personal preference scoring, dwell estimates, ordering, routing, timing.

### Notes and risks

- **This is a bigger release than it looks**, precisely because the base layer is near
  empty. It is also the first rung with a real per-plan cost.
- **Timezones and DST.** Store zoned datetimes from the first commit.
- **Last admission is invisible to every automated check.** A museum closing at 18:00
  with last entry at 17:00 makes a 17:30 arrival worthless, and no reliable structured
  tag exists in OSM or Places. The category prior is a guess — label it `assumed`.
- **Stale data.** Hours last touched in 2019 deserve lower confidence than hours touched
  last month; `check_date` is the cheap signal.
- **Multi-day comes free here.** R1's natural output is per-day, so a three-day trip is
  three columns. That open question only becomes hard at R4, where the route must be
  shaped.
- Verification results cache per POI *per date* and decay — a site checked three months
  ago for a date next week should be re-checked. This is where tier 3 fits.

---

## 6. R2 – R7

### R2 — Personal scoring

**Ships:** the shortlist ranked for *this* user rather than by generic prominence.

**Deliverables:** exterior and interior worth scores; category tags; dwell estimates per
variant; ticket price; preference input surface; the per-POI "why it's worth it" line;
an evaluation harness with frozen candidate sets and stored human labels.

**Notes:** split intrinsic scores (cached indefinitely per POI) from preference match
(cheap, per request) — this turns per-plan model cost from O(candidates) to O(new
candidates). Benchmark on a set that is **half long-tail**, not landmarks, or it will
flatter itself. Exterior scoring needs only "worth a detour: no / yes / definitely",
not a calibrated scale. Evaluation is a recurring tax, not a one-time gate: every prompt
change re-runs the panel, and the panel is human hours.

### R3 — Geographic grouping

**Ships:** clusters with rough time estimates and no timestamps. *"Alfama: these six,
about half a day. Belém: these four, take a morning — the monastery is shut Monday."*

**Deliverables:** spatial clustering; rough inter-cluster walk times; a per-cluster
summary view.

**Notes:** large value jump for small effort — no solver, no travel matrix, no objective
function. Many users may want exactly this and not a minute-by-minute schedule.

### R4 — The timed route

**Ships:** the spec's stated Goal.

**Deliverables:** travel-time matrix behind the `depart_at` interface; orienteering-with-
time-windows solver with optional nodes and pinned occurrences; **the objective
function**; dwell allocation; exterior-vs-interior as a solver decision; directions text;
multi-day via the multi-tour depot; meal and slack virtual stops; partial first and last
days with a departure buffer; the honest failure output for thin cases.

**Notes:** the objective function is the largest unspecified design problem in the spec —
converting `{exterior, interior, dwell, ticket price, preference}` into one scalar with
the right *shape* (diminishing returns per category, or you get five churches in a row;
a price budget, or one blockbuster museum eats the day). It has no library and no ground
truth, and it is the only deliverable here with no completion criterion. Give it its own
module.

### R5 — Scenic legs

**Ships:** the nicer path between consecutive stops, with a reason.

**Deliverables:** alternates enumeration and corridor scoring, or a pleasantness-weighted
costing profile; per-leg explanation; a slack-budget guard.

**Notes:** runs on the K−1 chosen legs, never N² — path geometry is a far heavier query
than duration. A detour spends already-allocated slack only, with a hard cap, and never
triggers a re-solve. Needs its own feasibility probe first: routing engines often return
near-identical alternatives for sub-1km urban legs, in which case the honest output is
"no scenic option here." A cheaper fallback exists once R2 has run — route past
high-exterior-score POIs you are not otherwise stopping at, which is grounded in data you
already trust.

### R6 — Interactive re-plan

**Ships:** the ability to change a plan and have it reflow.

**Deliverables:** a constraint model (lock, drop, retime, reschedule); a re-plan endpoint;
map and timeline affordances.

**Notes:** the same machinery absorbs "I am 40 minutes late" and "this place is shut."
Showing a plan on a map makes this close to mandatory rather than optional.

### R7 — Public transport

**Ships:** the third transport mode.

**Deliverables:** transit routing behind the matrix interface; reachability-isochrone
discovery radius; waiting-for-departure as its own segment type; per-city feed
availability check and an honest unsupported message.

**Notes:** last because it retro-fixes two earlier layers rather than extending one — the
radius stops being a circle and travel time stops being static. Feed availability varies
enormously; a city without one should say so rather than silently degrading to a walking
plan.

---

## 7. Spec gaps to resolve

Issues in `travel-planner-spec.md` that are assumed rather than stated. Each needs a
decision, not a workaround.

| Gap | Detail | Needed by |
| --- | --- | --- |
| **Arrival and departure are places** | Stage 3 routes "from arrival to departure", but Inputs collects only "Destination: City or area". There is no coordinate to route the first leg from. | R4 |
| **Lodging coordinate** | Multi-day with a base anchor needs one; Inputs does not collect it. | R4 |
| **Objective function** | Never mentioned; it is the hardest design problem in the system. | R4 |
| **"No filtering or ranking yet"** | Stage 1's stated contract is unaffordable — the quadratic matrix and per-candidate model cost both force a pre-filter, and prominence ranking moved into R0. This is a deliberate amendment, and it changes what recall guarantee Stage 1 can offer. | R0 |
| **Departure buffer** | An itinerary whose last stop ends five minutes before a flight passes every check. | R4 |
| **Ticket price** | Collected under Interior interest; nothing consumes it. Belongs in the objective function or as a budget input. | R4 |
| **Timezones and DST** | Every "arrival falls inside the open interval" check is a timezone comparison. | R1 |
| **Failure output** | Undefined for a four-hour layover, a Monday when everything is shut, or a city with no coverage. | R4 |
| **Mid-trip repair** | Absent from the spec. A minute-by-minute plan with no repair path is abandoned by 11am the first time something is shut. | R6 |

## 8. Open questions, with recommended answers

| Question | Recommendation | Blocks |
| --- | --- | --- |
| **POI data source** | Not decidable by argument — R0 settles it by measurement. Default to OSM plus Wikidata for discovery (free, cacheable, no display coupling), and a commercial source for hours at R1. Note that Google Places terms restrict persisting most fields beyond 30 days and require display on a Google map, which couples the map library to the POI source. | R0, R1 |
| **Multi-day** | One route per day with a base anchor. Build the solver multi-tour from its first version and this costs a parameter instead of a rewrite. Add a lodging coordinate to Inputs. | R4 |
| **Meals and breaks** | Opaque break blocks as virtual stops with soft windows. Do **not** do named restaurants — that needs a route-conditioned second discovery pass, inverting the pipeline. Separate feature if ever. | R4 |
| **Slack** | A solver parameter with a default (~15% of leg time, minimum 10 minutes), swept and then settled by whoever walks a generated day. Not answerable in the abstract. | R4 |

---

## 9. Validation approach

Three kinds of check, because the three sub-problems have nothing in common:

- **Invariants** — for anything deterministic. Every arrival inside its open interval, no
  overlapping stops, dwell plus travel plus slack reconciling to the trip window, nothing
  past the departure buffer. Cheap, exhaustive, permanent.
- **Ground truth** — for anything factual. Recall against the hand-written top-25;
  false-open rate against official websites. These have right answers; measure them.
- **Human judgment** — for anything the model decides. Frozen candidate sets so runs are
  diffable, forced-choice panels rather than absolute scores, and a benchmark weighted to
  the long tail. There is no right answer here, only stability and preference agreement.

Two things no automated check catches, both of which the whole product's credibility
rests on: **dwell estimates and slack**. The only validation that finds them is walking a
generated day and recording where the schedule drifted. Build the calendar export early
enough to make that possible.

**Adversarial inputs**, run as a permanent regression suite from R1 onward: a Monday, a
public holiday inside the window, a 22:00 arrival, a four-hour layover, a multi-day
window, a departure-buffer violation, and a city with sparse data.

**Progress will look like regression twice.** R1's honest hours delete attractive stops,
and R4's real travel times shorten the day. Anyone judging by "does today's output look
nicer than yesterday's" will conclude the project is going backwards. The invariant suite
is the honest progress signal.

---

## Appendix — measured data

Live measurements against Overpass, taginfo, and the Wikidata Query Service, August 2026.
Recorded because several were sharply different from the assumptions this plan started
with.

**Naive tag pull is unusable.** Paris returned 15,790 objects: 17% `tourism=artwork`, 17%
`tourism=information`, 16% `leisure=garden`, 13% `tourism=hotel`, 11% `historic=memorial`.
Museums and attractions together were ~3%. Prague 10,519; Kyoto 11,268 (27%
`tourism=information`); Mexico City 3,894 (49% `leisure=garden`); Marrakesh 1,361 (61%
accommodation). The curated whitelist cut Paris to 2,406 and Kyoto to 695.

**Opening-hours coverage**, pooled across five cities: `tourism=museum` 53.7%, `gallery`
23.3%, `attraction` 8.7%, `amenity=place_of_worship` 5.0%, `historic=monument` 2.8%,
`tourism=viewpoint` 2.1%, `historic=memorial` 0.1%. Whole-pull: Paris 3.6%, Prague 2.5%,
Kyoto 1.1%. On the curated whitelist: Paris 18%, Kyoto 12%, Mexico City 10%. Of the 50
most famous Paris POIs by sitelink count, 25 carried hours — and several of the rest are
open public spaces where hours do not apply.

**Wikidata tag coverage** on the named sightseeing subset: Paris 38.0%, Prague 40.0%,
Marrakesh 17.9%, Mexico City 14.7%, Kyoto 10.4% — a fourfold spread on identical filters.
By tag globally: `historic=castle` 50.2%, `building=church` 35.4%, `tourism=museum` 27.6%,
`amenity=place_of_worship` 14.3%, `tourism=viewpoint` 1.2%.

**QID defects** in the Paris pull of 3,239 distinct QIDs: 2.2% have no `P625` coordinate
because they point at a subject rather than an object (a QID for "metre" on a measurement
marker, for Alexandre Dumas on his statue); 1.2% are shared by multiple OSM objects, worst
case one QID on 37 metro entrances.

**Sitelink ranking works.** The measured Paris top-20 came out Eiffel Tower (189), Louvre
(169), Notre-Dame (124), Orsay (122), Champs-Élysées (94), Bastille (86), Arc de Triomphe
(85), Père-Lachaise (70) — and stayed credible through rank 75. Median is 3 sitelinks,
with 481 POIs at ≥5 and 239 at ≥10, so the signal thins quickly into the tail.

**Infrastructure limits.** Public Overpass allows 2 concurrent slots per IP, ~10k
requests/day, 180s default timeout; HTTP 429 arrived after two back-to-back city queries,
recovering in 30–120 seconds. Query timings: Paris 10.3s / 1.09 MB narrow, 25.2s / 5.00 MB
naive. Wikidata Query Service handled 800 QIDs in 7.4s but refused requests entirely
during an outage. Nominatim forbids autocomplete and caps at 1 request/second.

**Silent failure modes.** A timed-out Overpass query returns HTTP 200, valid JSON,
`"elements": []`, and a `remark` key reading `runtime error: Query timed out`. A partial
query returns partial elements plus the same remark. An area id derived from a
`place=neighbourhood` node returns HTTP 200 with zero results and no error. Observed
alongside these: HTTP 504 with an XHTML body, which breaks JSON parsing rather than a
status check. The main endpoint served data current to the minute while a mirror served
`2026-05-31` on one call and `2026-06-12` minutes later — the same mirror, up to nine
weeks stale, non-deterministically.

**Geometry.** Lisbon pull of 1,032: 548 ways, 141 relations, 343 nodes — only the nodes
carry top-level coordinates. With `out center`, 25 of 316 closed named ways (7.9%) place
their pin outside their own polygon, including Palácio Fronteira, Parque do Vale do
Silêncio, Jardim Mahatma Gandhi and Parque Telheiras. `out center geom` returns geometry
and no centre at all.

**City-boundary ambiguity.** Nominatim returns two results both displaying `Lisboa,
Portugal`: relation 5400890 (11.7 × 12.4 km, `addresstype=city`) and relation 2897141
(71.5 × 62.0 km). Naples repeats it at 13.9 km and 53.7 km. Against Lisbon city proper,
the curated whitelist returned zero hits for Sintra, Pena, Cascais, Estoril, Queluz and
Cristo Rei. Result shapes vary too: "Alfama" returns a node and a way, "Cambridge" a
relation and a node with a fourfold bbox difference, "Georgia" a US state and a country.

**Per-city whitelist drift.** Lisbon's dominant polluter is not Paris's. `historic=castle`
was 185 of 1,032 Lisbon elements (18%) — almost all `castle_type=palace|stately` private
townhouses. `tourism=attraction` (202) decomposed into 41 fountains, 17 ponds, 11
individual street trees, 5 bare rocks and 66 with no secondary tag. Nineteen elements
carried the excluded `historic=yes` by arriving through another predicate, including
Teatro Nacional Dona Maria II; 64 elements matched two or three whitelist families at
once.

**Ranking portability.** Lisbon's top POI by sitelinks is Torre de Belém at 57, against
the Eiffel Tower's 189 — so absolute cutoffs encode city fame. Lisbon survival after the
`P625` gate: ≥3 keeps 208 (20%), ≥5 keeps 113 (11%), ≥10 keeps 51 (5%), ≥20 keeps 14.
Of 1,032 Lisbon elements, 531 (51%) carry no Wikidata tag and therefore no rank at all.

**Presentation data is nearly absent from OSM.** Lisbon: `image` 0.3%, `website` 19.4%,
`description` 41.1% — the last in Portuguese, median 243 characters, one of them
free-text state (`Encerrado temporrariamente`). The `wikipedia` tag mixes language
prefixes (183 pt, 46 en, 4 de, 1 fr). The MediaWiki API returned 19 pages for 20 input
titles, keyed by pageid and unordered.

**Name localization.** Central Kyoto (759 elements): 3.7% of `name` values are pure
ASCII, `name:en` covers 31.0%, `int_name` 0.1%, romaji 2.2% — leaving 65.7% with no
Latin-script label from any tag. Lisbon carries `name:en` on 7.4%. Kyoto also embeds
English inconsistently inside the primary tag: `新熊野神社 (Imakumano Shrine)`,
`織成館(Orinasu-kan)`.

**Dedup shape.** Lisbon: 13 QIDs on more than one element (30 elements, 2.9%), against 30
names repeating across 65 elements with 22 same-name pairs within 150 m — most with no
QID. Unsafe QID merges observed: one QID spanning a cable-car line and both its stations,
one spanning a viewpoint and its adjacent garden, one being the species Tipuana tipu on
three separate street trees.
