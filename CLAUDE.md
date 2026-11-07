# CLAUDE.md

Guidance for Claude when working in this repository.

## Project context

**Orbit Diff** watches places for change from orbit. A user draws a polygon around an area they care about (an AOI), browses about ten years of **Sentinel-2** imagery over it as cloud-free monthly composites, sees what changed (new buildings and roads, forest loss, solar farms, water), and gets an email when a new satellite pass shows a change in an area they watch.

Imagery comes from the **Earth Search STAC API** (Element 84; `sentinel-2-l2a` as Cloud-Optimized GeoTIFFs on AWS), with **Microsoft Planetary Computer** as the fallback (ADR-002). Only the AOI's window is read, via HTTP range requests. Full 110 km tiles are never downloaded.

`mockup/orbit-diff.html` is the UX reference: layout, interactions, alert rules, the alert email, and the pipeline outline. Its imagery and events are synthetic. Match its behaviour, not its code. `docs/implementation_plan.md` sets the direction; `docs/ADR.md` records decisions and the reasons they changed.

The point of this project is the **path from a polygon to an alert someone trusts**: scene search → windowed reads → cloud masking → monthly composites → change detection → confirmation → alert → user feedback → better model. A change map is easy to make. One that doesn't cry wolf is the deliverable.

Two things distinguish this project and must not be quietly dropped:

1. **Alert honesty.** Clouds, haze, shadows, snow, crop cycles, floods, sensor and processing changes, and misregistration all look like change. An alert must survive cloud masking, a seasonal check, a minimum mapping unit, and confirmation on a later clear pass. A false alert costs more than a late one.
2. **Read only what you need.** Windowed COG reads, a per-AOI imagery cache, and bytes read measured on every job. The "data actually read" figure is shown to users; it's a product feature, not a debug stat.

## Core principles

- **Concise over clever.** Smallest amount of code that solves the problem clearly.
- **No premature abstraction.** Don't add layers, factories, or config switches until a second concrete use case exists. No Dask, no tiling cluster, no plugin system until an AOI actually needs it.
- **Best practices, not ceremony.** Follow Python and TypeScript idioms; don't import patterns from other ecosystems just because they're familiar.
- **Readable beats compact.** Concise is not the same as obfuscated. Raster code especially: name the axes, name the bands.
- **Delete more than you add.** When refactoring, the diff should usually shrink.

## Git workflow

- **Branch per change.** Never commit features or fixes directly to `main`. Use `feat/<slug>`, `fix/<slug>`, `chore/<slug>`, `docs/<slug>`.
- **One concern per branch.** If scope grows, split.
- **Commit messages: short, imperative, descriptive. No filler.**
  - Good: `feat: add Earth Search scene search for an AOI`
  - Good: `fix: apply BOA offset to scenes from processing baseline 04.00`
  - Good: `chore: pin rasterio and drop unused stackstac`
  - Bad: `updates`, `WIP`, `various changes`, `fixed stuff`
- **Squash noisy histories before merging.** Keep `main` linear and readable.
- **Never commit secrets.** Object-storage keys, catalog credentials, the email provider key and the feedback-link signing key are secrets. Provide `.env.example` instead.
- **Never commit imagery, rasters, or model weights.** Rasters live in object storage; models go through MLflow. Evaluation labels (small GeoJSON files) are the exception and live in git.

## Code quality

- **Python** (backend, workers, ML): type hints on public functions; `ruff` for lint, `ruff format` for formatting; `pytest` for tests.
- **TypeScript** (web app): strict mode; Biome for lint and format.
- **Dependencies:** pin them. `uv` for Python, `pnpm` for the web app. Keep heavy ML dependencies (PyTorch, TorchGeo) in their own dependency group so the API image stays small.
- **Configuration:** environment variables for secrets; TOML for pipeline parameters (SCL classes, thresholds, minimum mapping unit, AOI size cap, archive start, polling interval). No hardcoded URLs, thresholds, or magic numbers.
- **Logging over print.** Log AOI id, scene counts, AOI clear fraction, bytes read, job duration, catalog used, and model version. Never full arrays. Never AOI geometries next to user emails.
- **Tests where it matters.** Required for reflectance harmonization, cloud masking, compositing, CRS and area math, event extraction, the event status machine, and alert-rule evaluation. Test detectors on small synthetic rasters with known, staged changes; the mockup's synthetic landscape shows the idea.
- **Notebooks are scratch space.** If logic matures, move it into the package. Don't import from notebooks.

## Geospatial and change-detection practices

### The false-alert rule (most important rule in this repo)

A change can alert only if it is visible in **clear pixels of real observations** on both sides, falls **outside the seasonal envelope**, is **larger than the minimum mapping unit**, and, to be confirmed, **shows again on a later clear pass**.

- **Gap-filled months are display-only.** Interpolated pixels are never evidence of change.
- **Compare like with like.** Use anniversary composites (same month, different year) or a seasonal model (BFAST). Comparing July with January finds winter, not change.
- **One scene is a suspicion.** A change seen in one clear scene is `pending`. It becomes `confirmed` when the next clear observation over its footprint agrees, and `dismissed` when it doesn't.
- **Transient changes are labelled, not deleted.** A spring flood, snow, or a harvest that reverts is `seasonal`; users choose whether to hear about it.
- Any change to thresholds, mask classes, the minimum mapping unit, or the confirmation rule is re-run on the evaluation set before merge, with the change in false alerts per AOI-year reported.

### One implementation, used everywhere

- **One compositing path** for the archive backfill and for incremental new-scene updates. Two copies drift, and the drift looks like change.
- **One alert-rule evaluator,** in the backend. The web app asks the API for its preview ("3 of 7 past changes would have alerted"); it never re-implements the rule.
- **One band and asset-key mapping** per catalog, in one place.

### Imagery and radiometry

- **Processing baseline 04.00 (25 Jan 2022) added a −1000 offset to L2A reflectance.** Unharmonized, every AOI shows a fake change that month. Harmonize in one function driven by `s2:processing_baseline` and `earthsearch:boa_offset_applied`, with tests on both sides of the date (ADR-003).
- **Never apply `raster:bands` offsets.** Earth Search publishes −0.1 even on items whose pixels it already corrected; following it subtracts the offset twice. Planetary Computer and Earth Search's `c1` collection keep the offset in the pixels. Any new catalog gets checked against a known pass's pixel values, not its metadata.
- **Tile cloud cover is not AOI cloud cover.** `eo:cloud_cover` describes the whole 110 km tile. Use it only as a loose pre-filter; decide usability from the AOI's own pixels.
- **SCL masking** drops classes 0 (no data), 1 (saturated or defective), 3 (cloud shadow), 8 and 9 (cloud, medium and high probability), and 10 (thin cirrus). Class 11 (snow) is kept and flagged. A learned cloud model runs on top to catch the haze and thin cirrus SCL misses.
- **SCL is a 20 m class raster.** Upsample it to 10 m with nearest neighbour. Bilinear resampling invents class codes.
- **One pass counts once.** An AOI can sit in several MGRS tiles (even in two UTM zones), and Earth Search's `sentinel-2-l2a` holds both the original and the reprocessed version of 2018–2021 passes. Keep one item per pass: the newest processing baseline, and the tile in the AOI's own UTM zone where it has data (ADR-002).
- **Platforms differ.** Sentinel-2A, 2B and 2C are close but not identical radiometrically. Keep `platform` on every scene so a platform switch can be ruled out as a cause.
- **Partial scenes are missing data, not change.** Swath edges show up as no-data inside the AOI.

### Geometry and CRS

- **Store geometries in EPSG:4326** (PostGIS). **Compute in the AOI's UTM zone** at 10 m. Never measure area or distance in degrees.
- **Reproject once, on read.** Sentinel-2 tiles ship in their own UTM zone, so an AOI near a zone boundary gets scenes in two CRSs.
- **Pin the grid.** Every composite for an AOI shares one CRS, transform and shape, so month A and month B line up pixel for pixel.
- **Misregistration is real.** Scenes can shift by up to about a pixel against each other, more in older archive scenes. Road and roof edges flicker. Morphological cleanup and the minimum mapping unit exist for this; never alert on 1–2 pixel slivers.
- **Enforce the AOI size cap at the API.** The windowed-read design assumes small areas.

### Time handling

- **Store and compute acquisition times in UTC.** Month buckets use the UTC acquisition date.
- Convert to the user's locale only for display and email.

### Reading data

- **Windowed COG reads only.** Never download whole tiles or SAFE archives.
- **Fetch once, recompute many times.** Each AOI's per-scene window stack is cached in object storage (Zarr). Changing a mask or a detection threshold must not refetch imagery.
- **Measure bytes read** per job from the COG blocks the window touches, and log it.
- **Read SCL first.** It's about 1/50th of a pass's bytes; fetch the 10 m bands only for passes with clear pixels over the AOI.
- **Be a polite client.** Cache STAC searches, back off on 429 and 5xx, fall back to Planetary Computer only on failure, and log every fallback.

### Models and evaluation

- **Naive baseline first.** A thresholded NDVI difference between anniversary composites. Every detector is reported against it; a detector that can't beat it doesn't ship.
- **Never split randomly in space.** OSCD has an official train/test split by city; keep it. Feedback-label splits are by AOI, never by pixel or by event. Neighbouring pixels in train and test produce fantasy metrics.
- **Train on what you serve.** OSCD is Sentinel-2 L1C (top of atmosphere); serving reads L2A (surface reflectance), and only the bands we fetch. Either train on the served bands and an L2A-aligned input or measure the gap explicitly. Don't silently train on one and serve the other.
- **Report events, not just pixels.** Event-level precision and recall, false alerts per AOI-year, and detection delay (days from the first clear post-change pass to the alert). Pixel F1 alone hides what users feel.
- **Break results down** by category (buildings and roads, vegetation loss, solar and industry, water) and by hard case (cropland, floodplain, snow). Report where detectors fail. Don't pick segments that flatter the model.
- **Every training run goes to MLflow.** Params, metrics on the frozen evaluation set, dataset version, input bands, and the model with a signature. Untracked runs don't exist.
- **Promotion is gated, never automatic.** A candidate replaces the champion only if it doesn't raise false alerts per AOI-year on the evaluation set and doesn't lose recall beyond the set tolerance.
- **Pin versions explicitly** and set seeds for stochastic steps.

### Provenance

- **Every event records** the STAC item IDs it came from, the composite months compared, the detector and model version, and the thresholds in force. An event that can't be traced to its scenes can't be explained to a user or debugged.
- **"Not a real change" feedback** is stored with the event's full provenance. It is training data and the precision signal. It is also biased: users only see alerted events, so it measures precision, never recall.

### Alerts and email

- **Never send real email from dev or tests.** Mailpit locally.
- **Every alert email carries** before/after crops, size, method and confidence, status (pending or confirmed), and a signed one-click "Not a real change" link.
- **Idempotent sending.** An event alerts a user once, plus at most one follow-up when it is confirmed or dismissed. Job retries must not double-send.
- **An AOI around someone's home is personal data.** Users only ever see their own AOIs, events and feedback.

## Ask before acting

When unsure, stop and ask. Specifically:

- Changes to what counts as a change: thresholds, mask classes, the minimum mapping unit, the seasonal rule, the confirmation rule
- Anything that refetches imagery or recomputes composites for every AOI
- New imagery catalogs, data sources, or dependencies
- Anything that changes the promotion gate or auto-promotes a model
- Anything that sends email to real addresses
- Raising the AOI size cap or moving the archive start date
- Anything that rewrites git history on shared branches
