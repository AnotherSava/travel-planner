# Travel Itinerary Planner — Spec

## Goal

Given a destination and an arrival/departure window, produce a complete, timed
route through the city: which points of interest to visit, in what order, when
to arrive at each, and how to get between them.

## Inputs

Collected from the user before planning starts:

| Input | Notes |
| --- | --- |
| Destination | City or area |
| Arrival | Date + time |
| Departure | Date + time |
| Transport mode | `car` \| `public_transport` \| `walking` — ask explicitly |
| Preferences | Categories the user cares about (see Stage 2) |

Transport mode determines the search radius and the travel-time model, so it
must be known before Stage 1 runs.

## Stage 1 — Discovery

Find candidate points of interest within reach of the destination.

- Radius scales with transport mode: walking → tight, public transport →
  along transit lines, car → wide.
- Output is a raw candidate list; no filtering or ranking yet.

## Stage 2 — Scoring

For each candidate POI, determine:

- **Exterior interest** — is it worth seeing from the outside? (facade,
  setting, photo value)
- **Interior interest** — is it worth going in? Ticket price, expected time
  inside, whether the inside justifies the stop.
- **Opening hours** — for the specific dates of the trip, not generic hours.
  Includes holiday and seasonal closures.
- **Category tags** — museum, art, architecture, nature, etc.
- **Preference match** — score against the user's stated preferences.

Exterior and interior are scored separately on purpose: a POI can be a
worthwhile drive-by even when entering is skippable, and that changes how much
time it needs in the schedule.

## Stage 3 — Route construction

Build a single route from arrival to departure.

- Ordered sequence of stops with **timestamps** — when to arrive at each.
- Respects opening hours from Stage 2.
- Allocates dwell time per stop based on the interior/exterior scores.
- **Directions** between consecutive stops.
- **Scenic routing**: when several paths connect two stops, prefer the more
  beautiful one from a tourist's point of view rather than strictly the
  fastest. The walk between stops is part of the experience.

## Output

A timeline from arrival to departure: stop, arrival time, why it's worth it,
how long to stay, and how to get to the next one.

## Open questions

- Multi-day trips: one continuous route, or one route per day with a
  hotel/base anchor?
- Meals and breaks — scheduled explicitly, or left to the user?
- How much slack to build in between stops?
- Where does POI data come from (Places API, OSM, something else)?
