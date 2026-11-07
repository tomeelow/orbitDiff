# Implementation Plan: Orbit Diff — Satellite Change Detection and Alerts for Your Own Areas

> **Goal:** A production-shaped geospatial service. Draw a polygon, browse ten years of Sentinel-2 over it, see what changed, and get an email when a new pass shows a real change. Not a notebook that makes one pretty change map.
>
> **Headline target:** alerts a user would trust: **fewer than one false alert per watched area per year** on the evaluation set, sent within **one clear pass** of the change becoming visible, with every alert traceable to the scenes that produced it.
>
> **How to read this plan:** it sets direction, not contract. Tools and specific solutions can change during implementation when there's a reason to; record the reason in `docs/ADR.md`. `mockup/orbit-diff.html` sketches the features and how they fit together; it's an idea of the UI, not its final look.

---

## Architecture Overview

```
 Web app (React + MapLibre)   draw AOI · timeline · swipe A/B · NDVI · events · alert rules
      │ REST                              ▲ map tiles
      ▼                                   │
 FastAPI ─────── enqueue ──────┐       TiTiler ◄───────── composite COGs ────────────┐
 AOIs · rules · events         │                                                     │
      │                        ▼                                                     │
      │        Worker (job queue)                          Scheduler                 │
      │        1 STAC search: Earth Search → PC     ◄────── new scenes over          │
      │        2 windowed COG reads → AOI cube (Zarr)       watched AOIs             │
      │        3 cloud mask: SCL + cloud model                                       │
      │        4 monthly median composites ──────────────────────────────────────────┘
      │        5 detect: NDVI diff · CVA · BFAST · Siamese U-Net
      │        6 events → seasonal check → pending → confirmed
      │                        │
      ▼                        ▼
 Postgres + PostGIS ◄──── alert rules ────► Email: before/after crops, "Not a real change"
 AOIs · scenes · events · feedback                                │
      ▲                                                           │ one click
      └──────────────────────── feedback ◄────────────────────────┘
                                   │
                                   ▼
             evaluation set + retraining (MLflow) → gated promotion
```

**Core design principle:** read only the AOI's window, fetch it once, and make every downstream step (masking, compositing, detection) cheap to recompute from the cached cube. Detection is deliberately conservative: an event must survive cloud masking, a seasonal check, the minimum mapping unit, and a second clear pass before it's called confirmed. User feedback on alerts closes the loop, as the precision signal and as training data.

---

## Data Sources

| Data | What it is | Source | Access |
|---|---|---|---|
| **Sentinel-2 L2A** | Surface reflectance, 10 m, every ~5 days. The imagery | Earth Search STAC (Element 84), COGs on AWS | Free, **no key** |
| **Sentinel-2 L2A (fallback)** | Same product as COGs, on Azure (ADR-002) | Microsoft Planetary Computer (PC) STAC | Free; anonymous SAS token |
| **OSCD** | 24 Sentinel-2 image pairs with urban change labels | Onera Satellite Change Detection dataset (via TorchGeo) | Free, research use |
| **CloudSEN12** | Labelled Sentinel-2 cloud and shadow masks | CloudSEN12 dataset | Free; only if we train our own cloud model |
| **Hansen Global Forest Change** | Annual tree-cover loss, 30 m, global | University of Maryland / GLAD | Free; cross-check for forest-loss events |
| **Basemap labels** | Place names and roads for context | OpenFreeMap or Protomaps | Free |

### Sentinel-2 facts that shape the design

- **Bands we read:** B02, B03, B04, B08 (blue, green, red, NIR) at 10 m, plus SCL (scene classification) at 20 m. Everything the product shows comes from these: true color, NDVI, NDWI, and the masks.
- **Revisit:** about 5 days with two satellites, more often at higher latitudes where orbits overlap. Sentinel-2C launched in 2024 and took over from 2A in 2025. Cloud cover means many months have only one or two usable passes, and some have none.
- **Processing baseline 04.00 (25 Jan 2022)** added a −1000 offset to L2A digital numbers. Reflectance is `(DN + offset) / 10000`. Get this wrong and every AOI shows a "change" in January 2022.
- **Tiles are 110 km MGRS squares in their own UTM zone.** An AOI can touch several tiles, two UTM zones, and two overlapping orbits.

### Canonical records (use from day one)

```python
from dataclasses import dataclass
from datetime import date, datetime
from typing import Literal

from shapely.geometry import Polygon

@dataclass
class Scene:
    item_id: str                  # STAC item id, the provenance anchor
    catalog: Literal["earth-search", "planetary-computer"]
    acquired_utc: datetime        # ALWAYS UTC
    platform: str                 # sentinel-2a / 2b / 2c
    mgrs_tile: str
    processing_baseline: str      # drives the BOA offset
    tile_cloud_pct: float         # whole tile, coarse pre-filter only
    aoi_clear_frac: float         # from SCL + cloud model, over this AOI only

@dataclass
class ChangeEvent:
    aoi_id: int
    footprint: Polygon            # stored in EPSG:4326
    area_ha: float                # computed in the AOI's UTM zone
    category: Literal["built", "veg", "solar", "water"]
    status: Literal["pending", "confirmed", "dismissed", "seasonal"]
    method: str                   # "ndvi-diff" | "cva" | "bfast" | "siamese-unet"
    model_version: str | None
    confidence: float
    before_month: date
    after_month: date
    evidence_items: list[str]     # STAC item ids that showed the change
    first_seen_utc: datetime
```

---

## Phase 0 — Project Setup and Spikes

**Goal:** scaffolding, recorded decisions, and answers to the few questions that could reshape the design.

### Repository structure
```
orbit-diff/
├── src/orbitdiff/
│   ├── catalog/        # STAC search, Earth Search + Planetary Computer adapters, asset keys
│   ├── io/             # windowed reads, reflectance harmonization, AOI cube cache, bytes read
│   ├── masking/        # SCL + learned cloud mask
│   ├── composites/     # monthly median, gap flags, COG writing
│   ├── detection/      # baseline, CVA, BFAST, Siamese inference, events, categories, status
│   ├── alerts/         # rule evaluation, email rendering and sending, feedback tokens
│   ├── api/            # FastAPI app
│   ├── jobs/           # queue jobs (backfill, new scene) + scheduler
│   └── db/             # SQLAlchemy models, Alembic migrations
├── ml/                 # training + evaluation: cloud model, Siamese U-Net
├── eval/               # evaluation AOIs and event labels (small GeoJSON, in git)
├── web/                # React + TypeScript + MapLibre
├── config/             # pipeline parameters (TOML)
├── tests/
├── mockup/             # early UI sketch, synthetic imagery
├── docker/
├── docs/               # ADR.md, implementation_plan.md
├── .github/workflows/
├── .env.example
└── docker-compose.yml
```

### Tooling setup
```bash
uv init --package && uv add \
    pystac-client odc-stac rasterio rioxarray xarray zarr s3fs numpy scipy shapely pyproj \
    fastapi uvicorn pydantic-settings sqlalchemy geoalchemy2 alembic "psycopg[binary]" \
    rq redis jinja2 httpx loguru
uv add --group ml torch torchgeo lightning mlflow omnicloudmask
uv add --dev ruff pytest pre-commit

pnpm create vite web --template react-ts
cd web && pnpm add maplibre-gl terra-draw terra-draw-maplibre-gl-adapter @tanstack/react-query
pnpm add -D @biomejs/biome
```

### Pipeline parameters (one file, no magic numbers in code)
```toml
# config/pipeline.toml
[aoi]
max_km2 = 10                      # windowed reads assume small areas
archive_start = "2017-01-01"

[masking]
scl_drop = [0, 1, 3, 8, 9, 10]    # no data, defective, shadow, cloud med/high, cirrus
scl_flag = [11]                   # snow: kept, flagged
scene_min_clear_frac = 0.6        # a scene "counts" for the AOI above this

[detection]
min_mapping_unit_ha = 0.1         # floor under the user's slider
seasonal_revert_months = 3        # reverts within this → seasonal
confirm_on_next_clear = true

[schedule]
poll_every_hours = 3
```

### Spikes (half a day each, before committing to the design)

> **Answered on 2026-11-07:** see `docs/spikes.md` and ADR-002/003. The main changes:
> - Keep one item per pass from `sentinel-2-l2a`.
> - Harmonize the offset from baseline + flag, ignoring `raster:bands`.
> - Use Planetary Computer, not CDSE, as the fallback (accepted).
> - Read SCL first, and read concurrently.
1. **Archive coverage.** Check which Earth Search collection covers the AOI back to 2017 with consistent processing. `sentinel-2-c1-l2a` (ESA's reprocessed Collection 1) may be the better choice where it covers the period. Write down the answer.
2. **Offset metadata.** Confirm which item properties say whether the BOA offset was already applied, on both sides of 25 Jan 2022.
3. **Window read cost.** Read one 2 km² AOI for one scene from a European machine. Measure bytes, request count and wall time. Bytes are quantized by the COG's internal tile size, so small AOIs cost about the same as tiny ones; know the number.
4. **Fallback format.** Check what CDSE serves (asset format, auth, whether range reads are efficient). If it isn't COG, the fallback is slower and that's acceptable; Microsoft Planetary Computer is the other candidate.

### Architecture Decision Record (write before coding)
Create `docs/ADR.md` with these decisions and their reasoning:
1. **Earth Search STAC + windowed COG reads, not Google Earth Engine or full-tile downloads.** GEE would do most of this pipeline for us, which is exactly why it's not used: owning search, reads, masking and compositing is the project. Full-tile downloads cost ~1000× the bytes for a neighbourhood-sized AOI.
2. **Monthly median composites are the unit of analysis.** They absorb residual clouds and reduce noise. Single scenes are used for the "single scene" view and for confirming pending events.
3. **Classical detectors first, a deep model only where it earns its place.** Thresholded NDVI difference as the baseline, then CVA and BFAST for general and vegetation change; a Siamese U-Net for buildings, where spectral rules struggle. Every detector is compared to the baseline.
4. **Postgres + PostGIS is the system of record; object storage holds rasters** (Zarr cubes and COG composites); Redis backs the job queue. Nothing raster-shaped goes into Postgres.
5. **TiTiler for map tiles.** It gives tiles, band expressions (NDVI), colormaps and point queries (the hover readout) from COGs with no custom tile code. Pre-rendered PNG frames per AOI-month are the fallback if TiTiler turns out to be overkill for small AOIs.
6. **Poll STAC for new scenes on a schedule; don't subscribe to notifications yet.** Polling every few hours is simple, testable and fast enough for a ~5-day revisit. Push notifications can replace it later without changing the job.

---

## Phase 1 — Scene Search and Windowed Reads

**Goal:** given a polygon and a date range, produce a pixel-aligned stack of harmonized reflectance and SCL over the AOI, cached, with bytes read known.

> **As built (2026-11-07):** reads go through rasterio directly rather than odc-stac (ADR-009), so the pass choice, the SCL-first gate and byte accounting stay ours. `CPL_VSIL_CURL_ALLOWED_EXTENSIONS` is left unset because Planetary Computer hrefs carry a SAS query string. The cube is local Zarr under `data/` until object storage arrives in Phase 4. Try it with `orbitdiff fetch --aoi examples/aoi_kyiv.geojson --start 2021-06-01 --end 2021-09-30`.

### Search
```python
from pystac_client import Client

EARTH_SEARCH = "https://earth-search.aws.element84.com/v1"

def search_scenes(aoi_geojson: dict, start: str, end: str) -> list:
    catalog = Client.open(EARTH_SEARCH)
    search = catalog.search(
        collections=["sentinel-2-l2a"],
        intersects=aoi_geojson,
        datetime=f"{start}/{end}",
        query={"eo:cloud_cover": {"lt": 95}},   # tile-level; loose on purpose
    )
    return list(search.items())
```
Keep the tile cloud filter loose: a 90% cloudy tile can be perfectly clear over a 2 km² AOI. Usability is decided per AOI in Phase 2.

### Windowed, aligned reads
```python
import odc.stac

def load_aoi_cube(items, aoi_polygon, epsg: int):
    return odc.stac.load(
        items,
        bands=["blue", "green", "red", "nir", "scl"],   # Earth Search asset keys
        geopolygon=aoi_polygon,
        crs=f"EPSG:{epsg}",          # the AOI's UTM zone, fixed per AOI
        resolution=10,
        groupby="solar_day",         # one pass across several MGRS tiles counts once
        resampling={"scl": "nearest", "*": "bilinear"},
    )
```
Then, in one function: apply the BOA offset where the item's baseline and catalog metadata say so, and convert to reflectance.

GDAL settings that make range reads efficient (set them once for the worker process):
```
GDAL_DISABLE_READDIR_ON_OPEN=EMPTY_DIR
CPL_VSIL_CURL_ALLOWED_EXTENSIONS=.tif
GDAL_HTTP_MERGE_CONSECUTIVE_RANGES=YES
GDAL_HTTP_MULTIPLEX=YES
GDAL_HTTP_VERSION=2
AWS_NO_SIGN_REQUEST=YES
```

### Cache and accounting
- Write the AOI's per-scene cube to object storage as **Zarr**, appendable along time. Every later step reads from here; changing a threshold never refetches.
- **Bytes read:** sum the sizes of the COG blocks each window touches (rasterio exposes per-block byte sizes). Compare with the full asset sizes for the "data actually read" stat.
- **Fallback:** on repeated Earth Search failure, search Planetary Computer through the same interface, signing hrefs with its anonymous SAS token. Its pixels keep the +1000 offset (ADR-003). Map asset keys per catalog in one place and log every fallback.

**Deliverable:** `orbitdiff fetch --aoi aoi.geojson --start 2017-01-01` builds and caches the cube and prints scene count, scenes per year, bytes read vs full-scene bytes, and wall time.

---

## Phase 2 — Cloud Masking

**Goal:** a per-pixel clear mask for every scene and an honest AOI clear fraction.

1. **SCL first.** Drop classes 0, 1, 3, 8, 9, 10; flag 11 (snow). SCL is 20 m, so it's upsampled with nearest neighbour.
2. **A learned cloud model on top.** SCL misses thin cirrus, haze and some shadows, and those slip into composites as "change". Start with a pretrained model that runs on the bands we already read (OmniCloudMask takes red, green and NIR). Train a small CNN on CloudSEN12 only if the pretrained one falls short on our evaluation AOIs.
3. **Dilate cloud and shadow masks** by a pixel or two. Cloud edges are where masks fail.
4. **AOI clear fraction** per scene, stored on the `scenes` row. This drives the timeline bars and the "N of M scenes clear" source line.

**How to know it works:** a hand-checked set of ~100 scenes over evaluation AOIs, with "should be usable / not usable" labels. Report SCL alone vs SCL + model.

**Deliverable:** `masking/` producing clear masks, plus a small report of how many residual-cloud scenes each method lets through.

> **As built (2026-11-07):** `orbitdiff.masking` (SCL with no-data kept out of the dilation, OmniCloudMask v4.0 pinned in config, both dilated by `dilate_px`) and `orbitdiff mask --id <aoi>`, which runs the model only on passes it hasn't seen and stores its raw classes in `data/masks/<aoi>.zarr`. A composite refuses a pass with bands but no model output rather than quietly using SCL alone. `orbitdiff mask-report --id <aoi>` writes a contact sheet and a `passes.csv` to label; there are no labels yet, so the report doesn't yet say which method lets fewer cloudy scenes through. On the spike AOI (Jun–Sep 2021, 32 passes with bands) the model masks a further 12–22% of the AOI on 4 partly cloudy passes, mostly hazy cloud edges SCL leaves in.
>
> **Amended (2026-11-07, spike 5):** `clear_mask` also drops any pass whose offset flag contradicts its pixels (`masking.offset_suspect`: harmonized dark targets below −0.02). Such passes are recorded as `scenes.offset_suspect` and shown in the API's scene list. Earth Search flags some March–November 2022 items wrongly; left in, they shift those months' composites by up to +0.25 NDVI.

---

## Phase 3 — Monthly Composites

**Goal:** one cloud-free, pixel-aligned image per AOI per month, with honest gap flags.

```python
import xarray as xr

def monthly_composites(cube: xr.Dataset, clear: xr.DataArray) -> xr.Dataset:
    bands = cube[["blue", "green", "red", "nir"]].where(clear)
    comp = bands.resample(time="1MS").median()
    comp["n_clear"] = clear.resample(time="1MS").sum()
    comp["gap_filled"] = comp["n_clear"] == 0
    return comp
```

- **Per-pixel median** of clear observations. A pixel with no clear observation in the month stays empty.
- **Gap filling is for display only.** Empty months are filled from neighbouring months so the timelapse doesn't flash black, and marked `gap_filled`. Detection reads the unfilled values and never treats filled pixels as evidence.
- **Write one COG per AOI-month** (B, G, R, NIR as uint16 reflectance × 10000, plus `n_clear`) to object storage, for TiTiler.
- **Same function for backfill and for incremental updates.** A new scene recomputes only its month.

**Deliverable:** `composites/` writing COGs for the full archive of an AOI, with a test that composites from backfill and from scene-by-scene updates are identical.

> **As built (2026-11-07):** `orbitdiff.composites.update_composites` (CLI: `orbitdiff composites --id <aoi>`) writes `<YYYY-MM>.tif` for detection (blue, green, red, nir, n_clear, n_snow; empty where no clear observation) and `<YYYY-MM>_display.tif` for tiles (gaps filled per pixel from the nearest clear month on each side, at most `fill_max_months` away, with a `filled` band), plus `months.json` with each month's pass counts, clear scenes and observed/filled shares. Months are fingerprinted by their passes and mask settings, so only what changed is recomputed; the display fill reads the stored detection COGs, which is what makes backfill and pass-by-pass updates byte-identical (tested). Files sit under `data/composites/<aoi>/` until object storage arrives.

---

## Phase 4 — API and Tile Serving

**Goal:** everything the web app needs to browse an AOI's archive.

### Endpoints
```
POST /aois                         { name, geometry } → { id, area_ha, status: "backfilling" }
GET  /aois                         the user's AOIs ("Watching 3 areas")
GET  /aois/{id}                    stats: area, archive range, scenes indexed, % usable, bytes read
GET  /aois/{id}/progress           backfill progress ("Indexed 412 of 1,180 scenes")
GET  /aois/{id}/timeline           per month: n_total, n_clear, gap_filled
GET  /aois/{id}/ndvi               monthly mean NDVI for the AOI, or for one event's footprint
GET  /aois/{id}/scenes?month=…     single scenes for the "single scene" view
GET  /aois/{id}/events             change events with months, method, confidence, status
GET  /aois/{id}/alert-rule         { enabled, categories, skip_seasonal, min_ha }
PUT  /aois/{id}/alert-rule
GET  /aois/{id}/alert-preview      "N of M past changes would have alerted", from the backend rule
POST /events/{id}/feedback         { label: "not_real" | "real" }   (also via a signed email link)
GET  /health
```

### Tiles
- **Composites:** TiTiler's COG endpoints. True color via band selection and rescale; NDVI via a band expression and a colormap matching the mockup's ramp.
- **Single scenes:** TiTiler's STAC endpoints straight from the Earth Search item, with the same harmonization applied.
- **Hover readout:** TiTiler's point query, throttled.
- **Cache** tile responses (Redis or HTTP caching in front). Composites for past months never change, so cache them hard.

**Deliverable:** a FastAPI service plus TiTiler that can create an AOI, run its backfill as a job, and serve its timeline, stats, NDVI series and tiles.

> **As built (2026-11-07):**
> - `orbitdiff.api` serves `POST/GET /aois`, `GET /aois/{id}` (stats, including bytes read against the estimated cost of whole files, and the grid's corners for image overlays), `POST /aois/{id}/backfill` (retry or catch up), `/progress`, `/timeline` (with TiTiler tile URLs per month), `/ndvi`, `/scenes?month=` and `/health`. Events, alert rules and feedback arrive with Phases 7 and 9.
> - Single-scene images come from the cube through the API (`/aois/{id}/scenes/{item}/{style}.png`), not TiTiler's STAC endpoints (ADR-010).
> - The backfill is an RQ job (`orbitdiff worker`, a SimpleWorker): fetch → cloud model → composites → `scenes` and `months` rows, with progress on the AOI row. Records live in Postgres via SQLAlchemy and Alembic (`uv run alembic upgrade head`).
> - TiTiler 2.4.0 runs in compose. It reads `data/composites` read-only and loads the mockup's NDVI ramp from `docker/titiler/colormaps`. Tile URLs carry each month's version, so they are cached as immutable.
> - Before Phase 9 exposes anything: TiTiler's `url=` parameter can read any AOI's composites, so tiles must go through the API or signed URLs once AOIs belong to users.

---

## Phase 5 — Web App v1: Browse the Archive

**Goal:** the features of the mockup's left half and stats, on real imagery. This is the first milestone worth showing to someone.

### Features (in priority order)
1. **Draw an AOI** on a MapLibre map (Terra Draw polygon tool), with the area and the size cap shown while drawing.
2. **Timeline** of clear-scene counts per month, gaps hatched, A and B cursors, keyboard stepping, and play for a timelapse (preload the next frames).
3. **Layers:** true color and NDVI; source: cloud-free composite or single scene.
4. **Swipe A/B** between two months (two synced maps or one map with a clipped layer).
5. **NDVI chart** for the AOI mean, clickable to move cursor B.
6. **Stats card:** area, archive range, scenes indexed and % usable, data actually read.
7. **Hover readout:** coordinates, NDVI, month.

The mockup's visual language (tokens, type, layout) is a starting point, not a requirement: the UI may look different where that works better. It must work in light and dark themes and at phone width.

**Deliverable:** draw an area, watch the backfill progress, then browse ten years of it.

> **As built (2026-11-07):**
> - `web/` is Vite, React 19 and TypeScript (strict), with MapLibre 6 and Terra Draw; Biome, Vitest and pnpm. The theme is Hallmark's Rim, in dark and light (the system's choice until the user picks one), and it works at phone width.
> - Drawing shows the area against the cap from `GET /limits` and refuses to submit over it; the API still enforces the cap.
> - The archive strip shows all passes and clear passes per month (clear in green), hatches months with no clear pass, and draws the mean NDVI line under it. Clicking moves the active cursor; ←/→ step it, Shift jumps a year, Space plays one month per second while the next month's tiles are fetched ahead.
> - The default pair is the latest observed month against the same month a year earlier. A note under the map warns when a month is gap-filled or partly filled, or when A and B are different seasons.
> - Swipe is two synced maps (ADR-011). Month B can show the composite or one pass; the pass list gives each pass's AOI clear share or why it was left out.
> - The hover readout asks TiTiler for the pixel's NDVI and whether it was filled for display.
> - Basemap: OpenFreeMap, configurable (ADR-011). Changes and alerts are not in the UI yet: their backend arrives with Phases 7 and 9.

---

## Phase 6 — Evaluation Set and Naive Baseline

**Goal:** before tuning any detector, know how you'll measure it. False alerts are the product's main failure mode, so they're measured from day one.

### The evaluation set (`eval/`, in git)
- **20–50 AOIs** with hand-verified events: new buildings, roads, clear-cuts, solar farms, quarries, reservoirs. Each event labelled with category, footprint and the month it became visible.
- **Quiet AOIs** that only look like they change: cropland, floodplains, snowy areas, deciduous forest, airports with parked aircraft. These measure false alerts.
- Verify events visually against high-resolution historical imagery. Use that imagery for checking only; check its terms before using it for anything else.
- **Freeze it.** Changes to the evaluation set are versioned and reported with any metric change.

### Metrics
- **Event-level precision and recall.** A detected event matches a labelled one when footprints overlap and dates fall within a tolerance.
- **False alerts per AOI-year.** The number a user feels.
- **Detection delay:** days from the first clear post-change pass to the alert.
- **Per category and per hard case.** Report the failures as plainly as the wins.

### The baseline
Thresholded NDVI drop between anniversary composites (same month, previous year). It only catches vegetation loss and it's easy to fool, which is the point: every later detector must beat it.

### The benchmark table (the headline artifact)
| Detector | Event precision | Event recall | False alerts / AOI-yr | Median delay (days) |
|---|---|---|---|---|
| Naive NDVI diff (anniversary) | — | — | — | — |
| CVA (anniversary) | — | — | — | — |
| BFAST Monitor | — | — | — | — |
| Siamese U-Net (buildings) | — | — | — | — |
| **Production config** (all + seasonal + confirmation) | — | — | — | — |

Cross-checks outside our own labels: forest-loss events against Hansen GFC loss years, and the Siamese U-Net on OSCD's official test split.

**Deliverable:** `ml/evaluate.py` that runs any detector config over the evaluation set and prints the table, logged to MLflow.

> **As built (2026-11-07):**
> - `orbitdiff evaluate --method naive [--fetch]` (in `orbitdiff.evaluation`, not `ml/evaluate.py`) scores methods over `eval/*.geojson`. It prints the table with breakdowns by category and hard case, and logs to MLflow (local `mlflow.db`) with the set's version (a hash of the label files) and every threshold.
> - A detection matches a labelled event when they overlap by at least a quarter of the smaller footprint and its month falls between one month before and twelve after the labelled month (`[evaluation]` in config). Delay runs from the first pass after the labelled month that sees at least half the footprint clearly.
> - `orbitdiff.detection` holds the one detection path: anniversary composites, a 3 x 3 opening (slivers of 1–2 px go, rectangles keep their corners), the minimum mapping unit, footprints in UTM, heuristic categories, and per-event provenance (months compared, passes clear over the footprint). The naive NDVI drop is its first method.
> - Thresholds are provisional until the set is labelled and verified. **The labels are not in yet:** they need someone to verify events against high-resolution imagery (`eval/README.md`).
> - Labelling is in the web app, for admins: footprints drawn over the archive, with the detected changes hidden meanwhile, are saved straight to `eval/<area>.geojson` in this format. The API refuses a label it couldn't score (a change in the period's first year, a footprint outside the area).

---

## Phase 7 — Change Detection v1: Classical Detectors and Events

**Goal:** events with categories, statuses and provenance, from detectors you can explain in a sentence.

### Detectors
**Change vector analysis** on anniversary composites: the magnitude of the difference vector across bands, thresholded.
```python
import numpy as np
import xarray as xr

def cva_magnitude(before: xr.Dataset, after: xr.Dataset) -> xr.DataArray:
    bands = ["blue", "green", "red", "nir"]
    diff = xr.concat([after[b] - before[b] for b in bands], dim="band")
    return np.sqrt((diff ** 2).sum("band"))
```

**BFAST Monitor** on the monthly NDVI series: fit trend + harmonics on a stable history period per pixel, then flag a break when the moving sum of residuals in the monitoring period crosses its boundary. This is what separates "the field was harvested" (inside the seasonal envelope) from "the field became a construction site" (a break). An AOI is small enough to vectorize the fit across pixels with plain NumPy least squares. Use an existing Python port if it holds up; otherwise the core is ~100 lines.

### From change pixels to events
```python
from rasterio import features
from scipy import ndimage
from shapely.geometry import shape

def extract_events(change_mask, transform, min_area_m2: float):
    clean = ndimage.binary_opening(change_mask)        # drop 1-px misregistration slivers
    labels, _ = ndimage.label(clean)
    for geom, _ in features.shapes(labels.astype("int32"), mask=labels > 0, transform=transform):
        poly = shape(geom)                              # in the AOI's UTM CRS, so area is m²
        if poly.area >= min_area_m2:
            yield poly
```

### Categories (starting heuristics, replaced by learned ones only if they fail)
| Category | Signature after the change |
|---|---|
| Buildings & roads | NDVI falls below ~0.2, visible bands brighten, stays that way across seasons |
| Forest & vegetation loss | NDVI drops from forest or shrub levels to bare soil, outside the crop cycle |
| Solar & industry | Low NDVI, low reflectance in visible and NIR, all year |
| Water | NDWI rises above 0 |

### Status machine
```
new clear scene shows change ──► pending ──(next clear pass agrees)─────► confirmed
                                    └─────(next clear pass disagrees)───► dismissed
confirmed ──(reverts within seasonal_revert_months)──► seasonal
```
- Every transition records the scene that caused it.
- A change that recurs in the same month in earlier years is `seasonal` from the start.

**Deliverable:** `detection/` producing events with categories, statuses and full provenance, scored in the Phase 6 table. The web app's "Changes" layer and "Detected changes" list are wired to it.

> **As built (2026-11-07):**
> - `orbitdiff.detection` has four methods on one path: `naive` (NDVI drop), `cva`, `bfast` (per-pixel trend plus annual harmonic, least squares over the three years before the month, a break at 3σ below the fit; the MOSUM boundary is replaced by the confirmation rule) and `production` (any of the three, with rules). `orbitdiff evaluate` scores all four; `production` gets two rows, alerting when pending and only when confirmed.
> - Rules: a single clear pass must show the change (a change only in the median is an artefact). It is `seasonal` from the start if snow covers half its observed footprint in either month compared (`snow_frac`), or if the footprint looked like this in the same month of one of the last `seasonal_history_years` years, meaning neither the NDVI drop nor CVA tells them apart. That catches floods and crop rotations without hiding a building on last year's clearing. Otherwise it's `pending`, the next clear pass decides `confirmed` or `dismissed`, and a confirmed change that reverts within `seasonal_revert_months` becomes `seasonal`. A pixel belongs to one event a year (plus a 2 px ring); a snow event claims nothing, so what the snow hid shows once it melts.
> - `orbitdiff.alerts.should_alert` is the one alert-rule evaluator: the API's events list, the evaluation and (Phase 9) sending all call it. By default users hear about buildings and roads, vegetation loss, and solar and industry from 0.5 ha; water and "other" are opt-in, and seasonal and dismissed events never alert.
> - Events live in Postgres (`events`, migration 0003) with their footprint, months compared, history (each status with the pass or month that caused it), passes used, method version (a hash of method, `RULES_REVISION` and every threshold) and thresholds. The backfill's last stage re-runs detection over the archive and reconciles: each recomputed event keeps the stored row from its month that it overlaps most; a stored event no longer found is dismissed, never deleted.
> - API: `GET /aois/{id}/events` (with each event's alert verdict) and `GET /aois/{id}/events/{event}/{before|after}.png` (true-colour crops from the detection composites; unobserved pixels transparent). The web app draws footprints by category (pending dashed, seasonal and dismissed dimmed), marks months on the archive strip, and lists changes with before/after crops. Selecting one sets A and B to the months compared and fits the map to it.
> - On Grünheide (8.5 km², Jan 2017–Nov 2026, 1,406 passes) the production method finds the Tesla clearing in March 2020 (90 ha, confirmed) and the factory roofs in March 2021 (49 ha). Under the default rule it raises 67 alerts in 8.9 AOI-years when alerting on first sight, 52 when alerting only on confirmation. Many of those are small spring "buildings and roads" changes on fields. **Thresholds and category heuristics are provisional until a verified evaluation set exists.**

---

## Phase 8 — Siamese U-Net for Buildings (ML Track)

**Goal:** a learned detector where spectral rules struggle (new roofs on already-bare ground, dense urban infill), trained, tracked and promoted like a real model.

### Training
- **Data:** OSCD via TorchGeo (`torchgeo.datasets.OSCD`), with its **official city-level train/test split**. Never mix patches from one city across train and test.
- **Model:** a Siamese U-Net (FC-Siam-diff style), ideally initialized from a pretrained Sentinel-2 encoder (TorchGeo ships some) because OSCD is small.
- **Inputs:** only **B02, B03, B04, B08**, the bands we read at serving time.
- **Train–serve skew:** OSCD is L1C (top of atmosphere); we serve L2A (surface reflectance). Either normalize so the input distributions match or measure the gap: evaluate the trained model on our L2A evaluation AOIs and report the difference from OSCD-test numbers. Don't hide it.
- **Feedback labels** from "Not a real change" become extra negatives in later rounds, split by AOI.

### Tracking and promotion
```python
import mlflow

with mlflow.start_run(run_name="siamese_unet_rgbn_l2a"):
    mlflow.log_params(params | {"bands": "B02,B03,B04,B08", "eval_set": eval_version})
    ...
    mlflow.log_metrics({"oscd_test_f1": f1, "eval_event_precision": p,
                        "eval_event_recall": r, "eval_false_alerts_per_aoi_yr": fa})
    mlflow.pytorch.log_model(model, "model", registered_model_name="orbitdiff_buildings")
```

```python
def passes_gate(candidate, champion, baseline, recall_tolerance: float = 0.02) -> bool:
    return (
        candidate.false_alerts_per_aoi_yr <= champion.false_alerts_per_aoi_yr
        and candidate.event_recall >= champion.event_recall - recall_tolerance
        and candidate.event_f1 > baseline.event_f1
    )
```
Only a passing candidate gets the `champion` alias. The worker loads the champion by alias at start-up and records its version on every event.

### Inference
CPU is fine for small AOIs. Keep PyTorch in a separate worker image so the API image stays small.

**Deliverable:** a registered model that beats the baseline on buildings in our evaluation set, a documented gate, and per-event model versions.

---

## Phase 9 — Watching, Alerts and Feedback

**Goal:** the live loop. A new pass over a watched area becomes, when warranted, an email in someone's inbox.

### Accounts
Magic-link email sign-in. Alerts need a verified address anyway, and there are no passwords to store.

### The new-scene loop
```
scheduler (every few hours)
  └─ STAC search: new items since last check over the union of watched AOIs
       (group AOIs by MGRS tile so one search covers many)
  └─ per (AOI, new scene):
       fetch window → append to cube → mask → usable for this AOI?
       → recompute that month's composite
       → update pending events (confirm or dismiss)
       → run detectors → new pending events
       → evaluate alert rules → send emails (idempotent)
```

### Alert rules
Per AOI, as sketched in the mockup: on/off, categories, skip seasonal, smallest change (0.1–5 ha, never below the configured floor).
```python
def should_alert(event: ChangeEvent, rule: AlertRule) -> tuple[bool, str]:
    if not rule.enabled:
        return False, "alerts off"
    if event.status == "dismissed":
        return False, "dismissed"
    if event.status == "seasonal" and rule.skip_seasonal:
        return False, "seasonal"
    if event.category not in rule.categories:
        return False, "category off"
    if event.area_ha < rule.min_ha:
        return False, f"under {rule.min_ha} ha"
    return True, "alert"
```
The same function powers the "N of M past changes would have alerted" preview.

### The email
- Subject and lead say what and where in plain words, with size ("Possible forest clearing in My neighbourhood (0.6 ha)").
- **Before/after crops** as inline images (attached, not remote, since many clients block remote images), with the event outlined.
- Facts: size, NDVI before → after, method and confidence, status.
- "Open in Orbit Diff" deep link to the event, and a **signed one-click "Not a real change"** link that needs no login.
- A pending event says it's waiting for a second clear pass. A follow-up goes out only when it's confirmed or dismissed.
- Sending is idempotent per (event, user, kind); retries never double-send. Mailpit in dev.

**Deliverable:** draw an area, turn on alerts, and receive a real email after a real pass. "Not a real change" lands in the `feedback` table with the event's provenance.

> **As built (2026-11-07):**
> - Accounts (`orbitdiff.accounts`): `POST /auth/login` emails a link; `GET /auth/callback` creates the user on first sign-in, starts a 30-day session (an HttpOnly, SameSite=Lax cookie; only token hashes are stored) and sends the browser to the web app. Links work for 15 minutes, any number of times, because mail scanners open links first. Every AOI has an owner; any other AOI is a 404. `orbitdiff assign-aois` gives AOIs from before accounts an owner. See ADR-012.
> - Alert rules (`alert_rules`, migration 0004) with `GET/PUT /aois/{id}/alert-rule` and `POST .../preview`. The preview, the events list's verdicts and sending all go through `alerts.should_alert`. "Would have alerted" counts an event if any status in its history passed the rule, so a pending alert later dismissed counts as the false alarm it would have been: on Grünheide the default rule gives 67 of 1,604, 34 of them retracted.
> - Turning alerts on records `watching_since`: the newest pass already in the archive (now, if a backfill is still running). Only a status set by a later pass alerts.
> - The new-pass loop is the incremental backfill: `orbitdiff scheduler` queues it every `poll_every_hours` for watched AOIs (claimed on the AOI's stage, so it's never queued twice), and its last step is `notify.notify`. The STAC search per AOI covers only the current year; finished years are cached. Grouping AOIs by MGRS tile waits for many AOIs.
> - `notifications` holds one row per (event, user, kind), claimed and committed before sending: "alert", then at most one "update" when the event is confirmed, dismissed or found seasonal. A failed send is retried on the next run, up to `send_attempts`; a claim the process died holding is never resent.
> - The email has inline before/after crops with the footprint outlined (the same PNGs the app shows), size, NDVI, months compared, status, method version, a deep link (`#/areas/{id}/changes/{event}`) and a signed "Not a real change" link (HMAC, `ORBITDIFF_SIGNING_KEY`, 90 days). The link opens a page with one button that POSTs, rather than recording on GET: scanners prefetch links. `feedback` stores the event's provenance with each verdict; the app has the same button.
> - Not yet: rate limiting on `/auth/login`, a digest when many changes arrive at once, and TiTiler tiles behind the same ownership check (Phase 11).

---

## Phase 10 — Monitoring and the Feedback Loop

**Goal:** know when alerts stop being trustworthy, and do something about it.

### What to watch
1. **Alert precision from feedback:** the "not a real change" rate per detector and category, rolling 90 days. Remember it's biased toward alerted events and says nothing about recall.
2. **The frozen evaluation set,** re-scored for every model version and config change.
3. **Pipeline health:** scenes processed, time from acquisition to alert, bytes read per job, STAC errors and fallback usage, job failures and retries.

### Wire detection to action
- A category's precision falling below its threshold flags that detector for review and starts a retraining candidate with the new feedback labels. The candidate goes through the Phase 8 gate like any other.
- Thresholds are never loosened or tightened automatically in production. A human changes config, and the change is re-scored on the evaluation set.

Start with SQL over Postgres and a small internal admin page. Add Prometheus and Grafana only when that's outgrown.

**Deliverable:** an admin view of precision by detector and category, pipeline health, and the retraining flag.

> **As built (2026-11-07):**
> - Every backfill writes a `job_runs` row (migration 0005): status, passes read, passes left to retry, passes read from the fallback catalog, bytes read and emails sent. A run whose sending fails is recorded as failed but leaves the area ready: its imagery and events are fine.
> - `notifications.pass_utc` keeps the acquisition time of the pass that set the event's status, so the admin view shows the median time from pass to alert.
> - `GET /admin/summary` (`orbitdiff.monitoring`, plain SQL) and the web app's `#/admin` page: precision from "Not a real change" per method version and category over `precision_days`, pipeline health over `health_days`, and recent failed runs. Admins are the addresses in `ORBITDIFF_ADMIN_EMAILS`; anyone else gets a 403. The view is aggregate: AOI ids at most, never names, geometries or email addresses.
> - A category with at least `min_alerts` alerts and precision under `min_precision` is flagged "Review". Nothing acts on the flag: there's no retraining candidate to start until Phase 8, and thresholds stay a person's change, re-scored on the evaluation set. `[monitoring]` values are provisional.
> - Not yet: evaluation-set scores on the page (they live in MLflow, which the API image doesn't carry), STAC search errors as a figure (they're logged, and a search that fails fails the run), and retries per job (RQ retries aren't recorded).

---

## Phase 11 — Containerization and Deployment

### Docker Compose (the whole stack, one command)
```yaml
services:
  db:        { image: postgis/postgis:16-3.4, volumes: [pg:/var/lib/postgresql/data] }
  redis:     { image: redis:7 }
  minio:     { image: minio/minio, command: server /data }         # dev; R2/S3 in prod
  api:       { build: ., command: uvicorn orbitdiff.api.main:app --host 0.0.0.0, env_file: .env }
  worker:    { build: { context: ., dockerfile: docker/worker.Dockerfile }, command: rq worker }
  scheduler: { build: ., command: python -m orbitdiff.jobs.scheduler }
  titiler:   { image: ghcr.io/developmentseed/titiler }              # pin a version
  web:       { build: ./web }
  mailpit:   { image: axllent/mailpit, profiles: [dev] }
  mlflow:    { image: ghcr.io/mlflow/mlflow, profiles: [ml] }
volumes: { pg: {} }
```

### Hosting (direction, decide in Phase 11)
- **One VM** (e.g. Hetzner) running the compose stack behind Caddy for TLS. A few users and small AOIs don't need more.
- **Object storage** with no egress fees (Cloudflare R2) or next to the VM.
- **Data locality:** Earth Search's COGs live in AWS `us-west-2`. Reads from Europe pay latency per range request. Measure it in the Phase 0 spike; if backfills are too slow, run the worker near the data and keep the rest in Europe.
- **Email:** a transactional provider (Postmark, Resend or SES) behind one `send_email()`.
- **CI:** GitHub Actions for lint, tests and image builds on every PR; deploy by pulling images on the VM.
- **Backups:** nightly Postgres dumps to object storage. Composites and cubes can be rebuilt from STAC; the database can't.

**Deliverable:** `docker compose up` brings up the whole stack locally with no manual steps, plus a deployed URL.

> **As built, the local half (2026-11-07):**
> - `docker compose up --build` runs db, redis, mailpit, titiler, a one-shot `migrate` (`alembic upgrade head`; the app services wait for it), api (8000), worker, scheduler and web (8080). The one step before it is the signing key in `.env`: compose refuses to start without one, and there's no default to commit.
> - One `Dockerfile` for the Python services. The API, scheduler and migrations share an image without the `ml` group (about 1 GB, mostly GDAL and SciPy); the worker builds the same file with `--group ml` (about 2.4 GB). Both run as a non-root user and get no `.env`: settings come from compose's `environment`.
> - The app services mount `./data` and use the same database as development, so the stack and `uv run` see the same areas, and TiTiler keeps reading `./data/composites`.
> - The web image builds with pnpm and serves `dist/` from nginx, with `.mjs` served as JavaScript (MapLibre's worker).
> - On Linux, torch and torchvision come from PyTorch's CPU-only index (`[tool.uv.sources]`). The PyPI wheels carry CUDA libraries and Triton, which made the worker image 10.8 GB on a machine with no GPU. Same versions otherwise: OmniCloudMask gives identical classes with either build (6 real passes, every pixel).
> - CI checks the compose file and builds the API, worker and web images on every PR, and checks that the worker imports the cloud model on a CPU-only PyTorch.
> - Not yet (needs accounts and credentials): hosting, object storage, a real email provider, TLS, backups, deploying images from CI.

---

## Phase 12 — Documentation and Polish

### README sections
1. One paragraph: what it does, for whom, and why ten years of free imagery over your own street is interesting.
2. The architecture diagram (redrawn, exported as an image).
3. The **benchmark table**, with per-category and hard-case breakdowns.
4. **"Why it doesn't cry wolf":** masking, anniversary comparison, BFAST, minimum mapping unit, confirmation. A gallery of false alerts the pipeline catches, and of ones it still lets through.
5. **"Read only what you need":** bytes read vs full scenes for a typical AOI.
6. The L1C-vs-L2A train–serve skew and how it was handled.
7. Setup: clone → `.env` → `docker compose up`.
8. A GIF of the swipe and the timelapse.
9. "What I'd do with more time": three honest bullets (for example: Sentinel-1 radar to see through clouds, larger AOIs with tiling, a learned change classifier).

### Blog post (400–600 words)
Problem → approach → the one decision worth explaining (confirmation on a second clear pass, or the processing-baseline offset that fakes a change in January 2022) → one surprising finding from the evaluation set → link to the live demo.

### Demo video (90 seconds)
Draw an area, watch the backfill fill the timeline, play the timelapse, swipe across a change, open an event's NDVI series, then show the alert email and click "Not a real change".

---

## Common Pitfalls

**The January 2022 phantom change.** Processing baseline 04.00 added a −1000 offset to L2A values. Unharmonized, every AOI changes at once. Test both sides of the date.

**Tile cloud cover used as AOI cloud cover.** The tile can be 90% cloudy while your AOI is clear, and the reverse. Decide from the AOI's own pixels.

**Bilinear-resampled SCL.** Interpolating class codes produces classes that don't exist. Nearest neighbour only.

**Counting one pass twice.** Overlapping MGRS tiles deliver the same acquisition twice. Group by solar day.

**Area in degrees, or two CRSs in one stack.** Compute in the AOI's UTM zone on one pinned grid.

**Comparing different seasons.** July against January detects winter. Use anniversary composites or a seasonal model.

**Gap-filled pixels as evidence.** Interpolated months are for the timelapse, not for detection.

**Misregistration slivers.** One-pixel shifts along roads and roof edges look like thin strips of change. Morphological cleanup and a minimum mapping unit.

**Partial scenes.** Swath edges leave no-data inside the AOI. Missing is not changed.

**Random spatial splits.** Neighbouring pixels in train and test make any model look great. Split by city (OSCD) or by AOI (feedback).

**L1C-trained, L2A-served.** OSCD is top-of-atmosphere; the product reads surface reflectance. Normalize or measure the gap.

**Pixel F1 as the only metric.** Users experience events and false alerts, not pixels.

**Refetching imagery to change a threshold.** Cache the cube; everything after the read must be cheap to recompute.

**Double-sent emails on retry.** Make sending idempotent per event, user and kind.

**A precision dashboard nobody acts on.** A breached threshold must flag a review and start a gated retrain.

---

## Phase Summary

| Phase | Key Deliverable |
|---|---|
| 0 — Setup | Repo, ADR, pinned deps, compose skeleton, spike answers (coverage, offset, read cost, fallback) |
| 1 — Search + reads | Aligned, harmonized AOI cube cached as Zarr, bytes read measured |
| 2 — Masking | SCL + learned cloud mask, AOI clear fraction per scene |
| 3 — Composites | Monthly median COGs with honest gap flags, one path for backfill and updates |
| 4 — API + tiles | FastAPI + TiTiler: AOIs, backfill job, timeline, stats, NDVI, tiles |
| 5 — Web v1 | Draw, timeline, true color / NDVI, swipe A/B, NDVI chart, stats |
| 6 — Evaluation | Frozen evaluation AOIs, event-level metrics, naive baseline, benchmark table |
| 7 — Detection v1 | CVA + BFAST, events, categories, seasonal and confirmation statuses, provenance |
| 8 — Siamese U-Net | OSCD-trained building detector, MLflow, gated promotion |
| 9 — Alerts | Magic-link accounts, scheduler, alert rules, email with one-click feedback |
| 10 — Monitoring | Precision from feedback, pipeline health, retraining flag |
| 11 — Deploy | One-command compose stack + public URL |
| 12 — Polish | README, blog post, demo video |

---

## Tech Stack Summary

| Layer | Tool | Why |
|---|---|---|
| Imagery catalog | Earth Search STAC (`pystac-client`), Planetary Computer fallback | Free, no key or anonymous token, COGs that support range reads |
| Raster reads | `odc-stac`, `rasterio`, GDAL | Windowed, reprojected, pixel-aligned loads from STAC items |
| Arrays | `xarray`, NumPy, SciPy | Labelled time × band × y × x cubes; morphology and labelling |
| Imagery cache | Zarr on object storage | Appendable per-AOI cube; fetch once, recompute many times |
| Geometry | Shapely, pyproj, PostGIS | Polygons, UTM math, spatial queries |
| Cloud masking | SCL + OmniCloudMask (or own CNN on CloudSEN12) | SCL is free but misses haze and thin cirrus |
| Change detection | NDVI diff, CVA, BFAST Monitor, Siamese U-Net | Explainable classical detectors first, deep model for buildings |
| ML | PyTorch, TorchGeo, Lightning | OSCD loader, pretrained Sentinel-2 encoders |
| Experiment tracking | MLflow tracking + model registry | Lineage, gated `champion` alias |
| API | FastAPI + Pydantic | Typed endpoints, async where it helps |
| Tiles | TiTiler | Tiles, NDVI expressions, colormaps, point queries from COGs |
| Database | Postgres + PostGIS, SQLAlchemy, Alembic | System of record for AOIs, scenes, events, feedback |
| Jobs | Redis + RQ, a scheduler process | Backfills and new-scene jobs off the request path |
| Web app | React, TypeScript, Vite, MapLibre GL JS, Terra Draw, TanStack Query | Open-source map stack, polygon drawing, data fetching |
| Email | Jinja2 templates, transactional provider, Mailpit in dev | Inline before/after crops, safe local testing |
| Containerization | Docker + Docker Compose | One-command local stack, same images in prod |
| CI | GitHub Actions | Lint, tests and image builds on every PR |
| Dependency mgmt | uv (Python), pnpm (web) | Fast, reproducible |
| Code quality | ruff, Biome, pre-commit | One tool per language for lint and format |

---

### Suggested CV line
> *Built Orbit Diff, a geospatial service that watches user-drawn areas for change in Sentinel-2 imagery: STAC search and windowed COG reads (≈X% of full-scene bytes), SCL + CNN cloud masking, monthly composites, CVA/BFAST and a Siamese U-Net change detector with MLflow-gated promotion, served through FastAPI, TiTiler and a React + MapLibre app, with email alerts and a one-click feedback loop. Headline: Y false alerts per area-year at Z% event recall on a hand-labelled evaluation set.*
