# Candidate areas: none of these are verified

These 26 files are drafts to speed up labelling. Nobody has looked at imagery for them.
The locations, footprints and months come from public descriptions (press releases,
Wikipedia, project pages), several from memory and unchecked. No file has `verified_by`, so
`orbitdiff evaluate` reports "labels NOT verified" and nothing scored on them is a
benchmark result until a person has gone through every area as described in
[README.md](README.md#labelling).

Every area is a 2.4 km square (5.76 km², under the 10 km² cap), from 2017-01 to 2025-12.
Footprints are rough rectangles around the site, not outlines. Each event's `source`
property says where its date comes from.

## What a verifier checks in every area

1. **The square is in the right place.** Several coordinates are approximate. If the site
   is off-centre or outside the square, move the square. Keep it under the cap.
2. **Every change is labelled, not only the documented one.** An unlabelled real change
   counts as a false alert against every detector. Construction next door, a new road, a
   clear-cut, a mine edge or a new pond all need their own event.
3. **The month is the first month the change shows** in a clear pass, not the press date.
4. **The footprint is the changed area**, traced at 10 m, not the rectangle.
5. **Quiet areas really are quiet** for all nine years. If something did change, either
   label it or move the square.

To label in the app, create the area with the file's exact `aoi` polygon (`POST /aois`).
The API refuses a label file whose polygon differs from the drawn area. Saving from the app
rewrites the file and drops the `source` properties; keep them in the commit message or here.

## Areas with changes

Categories: 5 `buildings_roads`, 4 `vegetation_loss`, 4 `solar_industry`, 3 `water`.

| File | Change | Month | Why it's here | Check |
|---|---|---|---|---|
| `giga-texas` | buildings_roads | 2020-07 | Large, fast, well-dated build on open land | Grading may show before July; buildings and roads spread over 2021–2022 |
| `giga-shanghai` | buildings_roads | 2019-01 | Factory built in about ten months | Location least certain; Lingang has lots of other construction to label; cloudy |
| `tsmc-arizona` | buildings_roads | 2021-04 | Desert to fab, bright soil before and after | Phase 2 and 3 work (2022, 2025) are further events |
| `intel-ohio` | buildings_roads | 2022-07 | Farmland to earthworks: crop cycles before the real change | Location approximate; New Albany business park has other new buildings |
| `grunheide` | vegetation_loss, buildings_roads | 2020-02, 2020-07 | The project's reference example: clearing, then factory | Later clearings (late 2020, 2024 expansion) and the rail and road works |
| `dannenroder-forst` | vegetation_loss | 2020-12 | A narrow corridor (~80 m): tests minimum mapping unit and slivers | Corridor position and width; the motorway build after 2021 is likely its own event |
| `paradise-camp-fire` | vegetation_loss | 2018-11 | Burn scar with a known day; regrowth afterwards | Burned extent within the square; rebuilding after 2019 |
| `lytton-fire` | vegetation_loss | 2021-07 | Burn scar in steep terrain with shadows | Village vs slopes; summer smoke haze around the date |
| `weesow-willmersdorf` | solar_industry | 2020-03 | Solar on cropland, 164 ha | Location approximate; the park's exact outline |
| `nunez-de-balboa` | solar_industry | 2019-03 | 1,000 ha plant: the square covers only part of it | Location approximate; panels went in over 2019, so the month varies across the footprint |
| `witznitz` | solar_industry | 2022-10 | Solar on a former lignite mine, bare ground before | Built in phases to mid-2024; may need one event per phase |
| `northvolt-ett` | solar_industry | 2018-06 | Industry with snow half the year | Location approximate (Bergsbyn industrial area); several build phases |
| `gerd-reservoir` | water | 2020-07 | Reservoir filling in the rainy, cloudy season | Square placement upstream of the dam; later fillings (2021–2023) extend the water |
| `kakhovka-reservoir` | water | 2023-06 | Water loss after the dam was destroyed | The categoriser names only water gain, so this one tests matching, not category; fast willow regrowth on the bed afterwards |
| `hasankeyf-ilisu` | water | 2020-02 | Valley flooded by a new reservoir | Month of first water in the square; New Hasankeyf town (built 2017–2019) may be inside |

## Quiet areas (no events)

| File | Hard cases | Why it's here | Check |
|---|---|---|---|
| `beauce-cropland` | cropland | Large fields, harvest and ploughing cycles | No new farm buildings or roads |
| `garden-city-pivots` | cropland | Centre pivots switching crops and fallow | No new pivots or feedlots |
| `rosetown-prairie` | cropland, snow | Prairie fields under snow each winter | No new bins, roads or pipelines |
| `unteres-odertal` | floodplain | Polders flooded in winter and spring | No new dikes or works on the river |
| `donau-auen` | floodplain, deciduous_forest | Floodplain forest, flooded in spring | No river-engineering works |
| `hardangervidda` | snow | Long snow season, late melt | No new cabins or roads |
| `seiser-alm` | snow | Alpine pasture under snow, hay cut in summer | No new hotels, lifts or roads |
| `hainich` | deciduous_forest | Protected beech forest, leaf-off winters | 2018–2020 drought damage may be real loss |
| `cataloochee` | deciduous_forest | Appalachian broadleaf forest, strong autumn colour | No fire or storm damage in the period |
| `madrid-barajas` | airport | Bright apron with parked aircraft changing daily | No apron or terminal works |
| `schiphol-polderbaan` | airport | Runway and grass in polder land | No new taxiways or buildings |

## How the files were made

A one-off script built each square and rectangle in the area's UTM zone and wrote them with
`evaluation.dump_aoi`. `tests/test_evaluation.py` checks that the committed set loads with the
same loader `orbitdiff evaluate` uses, that every area fits the cap, and that every event
lies inside its area. No imagery was fetched.
