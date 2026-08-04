---
name: boundary_is_reach_not_administrative
description: Query POIs by distance from the city centre, never by the municipal admin relation
metadata:
  type: project
---

The city boundary in this project is **reach, not municipality**. Discovery queries a
radius from the centre (currently 25 km, `SEARCH_RADIUS_KM`), never the Overpass
`area()` of the admin relation.

**Why:** an area-restricted query cannot *return* what sits outside the city line, so
no downstream filter can recover it. Measured losses under the municipal boundary:
Capilano Suspension Bridge and Grouse Mountain (District of North Vancouver), the
Museum of Anthropology and Wreck Beach (UBC, unincorporated Electoral Area A), The
Henry Ford — Michigan's most-visited attraction, ~1.8M visitors/yr — in Dearborn, and
the city-owned Detroit Zoo in Royal Oak. Vancouver recall went 9/15 to 14/15 on this
change alone.

A radius cannot be tuned per-attraction either: for Detroit, 16 km catches Dearborn
but not the Zoo, 18 km catches the Zoo but not Cranbrook, and 35 km sweeps in half of
Oakland County. 25 km covered every measured case except Cranbrook at 32 km, which is
genuinely a separate day trip.

**How to apply:** what belongs on a city's map is being close enough to reach and
worth reaching, not which council collects the taxes. `inside_boundary` survives as a
*display* label only — populated by a separate ids-only query — so the UI can say
"North Vancouver, 11 km" rather than implying everything is in town.

The accepted cost is a noisier ranking: Vancouver went from 693 to 2,190 POIs, and
suburban civic buildings (Port Moody City Hall, a public library, the airport) now
outrank Stanley Park on Wikidata sitelinks. Distance therefore needs to count
*against* prominence, which is still open.
