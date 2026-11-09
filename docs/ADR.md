# Architecture Decision Record

Decisions that are expensive to reverse, recorded with the reasoning behind them. Each
entry names the alternatives considered and what would change our mind. Evidence from the
Phase 0 spikes is in `docs/spikes.md`.

---

## ADR-001 — Earth Search STAC and windowed COG reads, not Earth Engine or full tiles

**Status:** accepted · **Date:** 2026-11-07

### Context

An AOI is a neighbourhood: a few km². A Sentinel-2 tile is 110 km on a side, and one
pass of the four 10 m bands plus SCL is ~850 MB as files. Over ten years an AOI sees
~1,400 passes.

### Decision

Find scenes through a STAC API and read only the AOI's window from Cloud-Optimized
GeoTIFFs with HTTP range requests. Don't download whole tiles or SAFE archives, and
don't use Google Earth Engine.

### Reasoning

- Measured cost of a window read is ~6 MB per pass, or 0.7% of the full assets
  (spike 3). Full-tile downloads would move ~1.2 TB per AOI backfill instead of ~8 GB.
- Earth Engine would run most of this pipeline for us. That's exactly why it's out:
  owning search, reads, masking and compositing is the project, and Earth Engine's terms
  complicate running a public service on it.

### Consequences

- Read latency, not bandwidth, dominates. Backfills need concurrent reads, and reading
  SCL first lets cloudy passes be skipped cheaply.
- We own harmonization across processing baselines (ADR-003) and pass deduplication
  (ADR-002).

### What would change our mind

AOIs growing to hundreds of km², where full-tile processing near the data becomes
cheaper than many small windows.

---

## ADR-002 — Catalogs: Earth Search `sentinel-2-l2a` first, Planetary Computer as fallback

**Status:** accepted · **Date:** 2026-11-07

### Context

The plan named Earth Search as primary and Copernicus Data Space (CDSE) as fallback. The
spikes turned up three facts that bear on both choices:

1. **Collection 1 has holes.** Earth Search's `sentinel-2-c1-l2a` has uniform processing
   but no 2017 data and only 26 items in 2022 over our AOI.
2. **`sentinel-2-l2a` has duplicates.** It covers 2017 to today but holds up to four
   items per pass: two tiles in different UTM zones, times the original and the 05.00
   reprocessing.
3. **The fallbacks differ in format.** CDSE serves JPEG 2000 behind S3 credentials.
   Planetary Computer serves the same product as COGs with a free anonymous token, and
   read the same window in 0.7 s against Earth Search's 10.3 s from this machine.

### Decision

- **Primary:** Earth Search `sentinel-2-l2a`. Keep exactly one item per pass:
  - the newest processing baseline when a pass exists twice;
  - the tile in the AOI's own UTM zone, with the other tile used only where the first
    has no data.
- **Fallback:** Planetary Computer `sentinel-2-l2a`, **not CDSE**. It's used through the
  same catalog interface, with its own asset-key mapping and SAS token handling.

### Reasoning

- Picking the newest baseline per pass makes `sentinel-2-l2a` effectively Collection 1
  (05.00 reprocessing for 2018–2021, 05.09 and later from late 2022) with the 2017 and
  2022 holes filled. That gives us one collection, one asset-key scheme and one bucket.
- A fallback that serves JPEG 2000 behind credentials doesn't really work as a fallback
  for a windowed-read design. Planetary Computer serves the same ESA product as COGs.

### Consequences

- Radiometry is still mixed in two places: 2017 (non-ESA processor, baseline `00.01`)
  and 2022 (04.00 and 05.10). The ~2% difference between baselines (spike 2) has to stay
  well below detection thresholds. Comparisons that cross 2017/2018 or 2021/2022 need a
  test on the evaluation set.
- Planetary Computer keeps the +1000 offset in its pixels; Earth Search mostly doesn't.
  ADR-003 has to cover both.
- **Open question for Phase 11:** Planetary Computer was ~10× faster to read from here.
  If the deployment VM ends up in Europe, Planetary Computer as *primary* is likely the
  better choice. Measure from the real host before deciding.

### What would change our mind

- Element 84 stops updating `sentinel-2-l2a`, or `c1` fills 2017 and 2022: switch the
  primary to `c1`.
- Planetary Computer's data APIs are retired or rate-limit us: go back to CDSE and accept
  slower JPEG 2000 reads.

---

## ADR-003 — Harmonize reflectance from baseline and offset flag; ignore `raster:bands` offsets

**Status:** accepted · **Date:** 2026-11-07

### Context

Processing baseline 04.00 (25 Jan 2022) added a −1000 offset to L2A digital numbers.
Getting it wrong shows up as a ~0.1 reflectance step across the AOI: a change that never
happened. Earth Search items describe the offset in two places, and those two places
disagree.

### Decision

One function converts DN to reflectance for every item, from any catalog:

```
offset_dn = -1000  if processing_baseline >= "04.00" and not boa_offset_applied  else 0
reflectance = (DN + offset_dn) / 10000
```

`boa_offset_applied` is `earthsearch:boa_offset_applied` on Earth Search, and false for
catalogs that don't remove the offset (Earth Search `c1`, Planetary Computer). The
`raster:bands` offset is never used.

**Amended 2026-11-07 (spike 5):** the flag is wrong on some items, so a per-pass pixel
check backs it. A pass whose dark targets read below −0.02 reflectance after harmonizing
had the offset subtracted from pixels that didn't carry it. It's marked `offset_suspect`
and gets no clear pixels, so no composite uses it.

### Reasoning

Spike 2 checked the metadata against pixel values, using the same pass in three
processing versions:

- Earth Search `l2a` items with `boa_offset_applied=True` read within ~2% of the
  pre-04.00 version.
- The `c1` version reads exactly 1000 DN higher.

So Earth Search already removed the offset from those items, while still publishing
`raster:bands` offset −0.1. Following that metadata would subtract the offset twice.

Spike 5 then found the flag itself unreliable. Unflagged 04.00 items from late January
and February 2022 do carry the offset, while unflagged items from mid-March 2022 on
don't. Planetary Computer agrees on both. No metadata or date rule covers both without
guessing from one tile, so the rule stays and each pass's own pixels check it. Left
unchecked, two such passes in April 2022 would have shifted that month's composite by
+0.25 NDVI over most of the Kyiv AOI.

### Consequences

- The function gets unit tests with fixtures from the versions in spike 2: pre-04.00,
  04.00 unflagged, 05.x flagged, `c1`, and Planetary Computer.
- A new catalog can't be added without checking a known pass's pixels against this rule.
- The pixel check sees one direction only: an offset subtracted twice drives dark targets
  below zero, but an offset left in looks like a bright surface. Over bright AOIs a wrongly
  flagged pass can slip through.

### What would change our mind

Earth Search fixing its `raster:bands` metadata or its flags doesn't change the rule: the
flag and baseline still decide, and the check stays as a guard. A catalog that harmonizes
differently gets its own documented branch, backed by a pixel check. A wrongly flagged
pass found over a bright AOI would move the check from dark targets to a comparison with
the AOI's other passes.

---

## ADR-004 — Monthly median composites are the unit of analysis

**Status:** accepted · **Date:** 2026-11-07

### Decision

Detection and browsing work on per-pixel monthly medians of clear observations. Single
scenes are used for the "single scene" view and to confirm or dismiss pending events.
Months with no clear observation are gap-filled for display only and never used as
evidence.

### Reasoning

- A median absorbs the residual cloud, haze and shadow that masks miss, and cuts the
  number of comparisons from ~1,400 passes to ~118 months.
- A month is short enough that a construction start or a clear-cut lands in the right
  place on the timeline.

### Consequences

- Detection is at best monthly. Confirmation on the next clear single scene is what keeps
  alerts timely.
- A change that happens and reverts within a month is invisible. That's acceptable for
  this product.

### What would change our mind

Users caring about sub-monthly events (fires, floods while they happen), which would call
for scene-level detection with a much stricter cloud model.

---

## ADR-005 — Classical detectors first; a deep model only for buildings

**Status:** accepted · **Date:** 2026-11-07

### Decision

- **Baseline:** thresholded NDVI difference between anniversary composites. It's
  reported for every result.
- **Production detectors:**
  - change vector analysis on anniversary composites;
  - BFAST Monitor on the monthly NDVI series;
  - a Siamese U-Net trained on OSCD, for buildings only.

### Reasoning

- CVA and BFAST are explainable in one sentence, need no training data, and BFAST models
  the seasonal cycle directly. That cycle (crops, snow, floods) is the main source of
  false change.
- New roofs on already-bare ground and dense infill are where spectral rules struggle.
  That's where a learned model earns its training and promotion cost.

### Consequences

- OSCD is Sentinel-2 L1C and we serve L2A. The skew is either normalized away or
  measured and reported (Phase 8).
- Each detector's events are tracked separately, so precision can be measured per
  detector.

### What would change our mind

The classical detectors failing the false-alert target on the evaluation set in
categories other than buildings.

---

## ADR-006 — Postgres + PostGIS for records, object storage for rasters, Redis for jobs

**Status:** accepted · **Date:** 2026-11-07

### Decision

- **Postgres + PostGIS** is the system of record: AOIs, scenes, events, alert rules,
  feedback.
- **Object storage** holds rasters:
  - each AOI's per-pass window cube as Zarr, appendable along time;
  - one COG per AOI-month for tiles.
- **Redis** backs the job queue (RQ) and the cache.

### Reasoning

- AOIs and events are geometries queried spatially, which is exactly what PostGIS is for.
- Rasters don't belong in a relational database.
- The cube is small (the spike AOI is 135 × 197 pixels; ~1,400 passes × 4 bands × uint16
  is ~150 MB uncompressed) and lets every step after the read be recomputed without
  refetching.

### What would change our mind

Job volume or retry semantics outgrowing RQ (move to Dramatiq or a Postgres-backed queue).

---

## ADR-007 — TiTiler serves map tiles

**Status:** accepted · **Date:** 2026-11-07

### Decision

TiTiler serves tiles from the per-AOI-month COGs, and from Earth Search items for the
single-scene view.

### Reasoning

TiTiler covers band selection, NDVI expressions, colormaps and point queries (the hover
readout) with no custom tile code.

### What would change our mind

TiTiler turning out to be overkill for areas this small. Pre-rendered PNG or WebP frames
per AOI-month would cover the timelapse and swipe with less machinery.

---

## ADR-008 — Poll STAC for new scenes; no push notifications yet

**Status:** accepted · **Date:** 2026-11-07

### Decision

A scheduler searches for new items over watched AOIs every few hours (`config/pipeline.toml`),
grouping AOIs by MGRS tile so one search covers many.

### Reasoning

- With a pass every few days, a few hours of polling delay doesn't matter.
- Polling is simple to test and replay, and it works the same against either catalog.
- Push notifications would tie us to one catalog's infrastructure.

### What would change our mind

Polling load on the catalog becoming a problem at many AOIs, or users wanting alerts
within the hour of ingestion.

---

## ADR-009 — Read windows with rasterio directly, not odc-stac

**Status:** accepted · **Date:** 2026-11-07

### Context

The plan sketched Phase 1 with `odc.stac.load(..., groupby="solar_day")`. Building it showed
three things the pipeline must control that odc-stac decides for us: which item stands for a
pass (ADR-002), whether a pass's 10 m bands are fetched at all (only after its SCL shows clear
pixels over the AOI), and how many bytes each read cost.

### Decision

Read each asset with rasterio through a `WarpedVRT` onto the AOI's fixed grid
(`orbitdiff.reading`). Pass choice lives in `catalog.group_passes`, the SCL-first gate in
`reading.read_pass`, and bytes come from the COG block sizes under the window.

### Reasoning

- `groupby="solar_day"` mosaics every item of a day, so both processing versions of a
  2018-2021 pass and both UTM zones get blended rather than one being chosen.
- odc-stac loads all requested bands per item; the SCL-first gate would need a second load.
- The direct path is ~100 lines and has no dependency beyond rasterio, which we need anyway.

### Consequences

- Concurrency is ours: passes run in a thread pool, and a pass's four bands in parallel.
  Measured on the spike AOI (Jun-Sep 2021, 49 passes): 117 s and 189 MB, against 191 s with
  bands read one after another.
- A failed read falls back to the pass's next-best item instead of failing the load.

### What would change our mind

Larger AOIs or many-band products where dask-chunked lazy loading pays off.

---

## ADR-010 — Single-scene images come from the cube, not TiTiler's STAC endpoints

**Status:** accepted · **Date:** 2026-11-07

### Context

ADR-007 planned single-scene views through TiTiler's STAC endpoints, reading each item's
COGs from Earth Search on every view and applying the BOA offset in a tile expression.

### Decision

The API renders a pass as a PNG over the AOI's grid, straight from the cube
(`GET /aois/{id}/scenes/{item}/{style}.png`). The web app places it with the grid's
four corners. Composite months still go through TiTiler.

### Reasoning

- It shows exactly the pixels the pipeline analysed, harmonized by the one function that
  does it (ADR-003), instead of a second harmonization written as a tile expression.
- Viewing a scene reads nothing from the catalog again; the bytes were paid once.
- AOIs are at most a few hundred pixels across, so a single image is enough at any zoom
  the map allows.

### What would change our mind

AOIs large enough that a single image is too heavy, or a need to show bands the cube
doesn't hold.

## ADR-011 — OpenFreeMap basemap, and swipe as two synced maps

**Status:** accepted · **Date:** 2026-11-07

### Context

The web app needs a basemap under the AOI's imagery, in a dark and a light style, and a
way to compare month A with month B side by side.

### Decision

- The basemap is OpenStreetMap data served as vector tiles by OpenFreeMap (`dark` and
  `positron` styles), with no key. `VITE_BASEMAP_DARK` and `VITE_BASEMAP_LIGHT` point it
  elsewhere. Only the basemap comes from there; every pixel of imagery comes from our API
  and TiTiler.
- Swipe A/B is two MapLibre maps stacked and kept in step. The top one shows A and is
  clipped at the divider; the bottom one shows B and takes all input.

### Reasoning

- No key and no account means no secret in the web bundle and nothing to set up locally.
- Two maps keep each month in its own layer stack, so A and B can differ in source (a
  composite against a single pass) without per-layer clipping, which MapLibre doesn't
  offer.

### What would change our mind

OpenFreeMap's terms or availability changing, or deployment needing a basemap we host
ourselves (Protomaps on object storage is the likely swap; only the two URLs change).

---

## ADR-012 — Sign-in by emailed link, server sessions, and alert emails sent at most once

**Status:** accepted · **Date:** 2026-11-07

### Context

Phase 9 needs accounts (an AOI around someone's home is personal data), a verified
address for alerts, and emails that are never sent twice and never sent about history.

### Decision

- Sign-in is a link emailed to the address: no passwords. The link token and the session
  token are random; only their SHA-256 hashes are stored. The session is an HttpOnly,
  SameSite=Lax cookie checked against the `sessions` table on every request, so signing
  out or deleting a user ends it at once.
- A sign-in link works for 15 minutes, any number of times. A feedback link is an HMAC over
  ids only (no email address in any URL), valid 90 days, and its GET only shows a button
  that POSTs.
- Sending claims a `notifications` row (unique per event, user and kind) in its own
  transaction before talking to the mail server. A failed send is retried up to a limit; a
  claim that never finished is never retried.
- Alerts start from the newest pass in the archive when they are turned on.

### Reasoning

- Mail scanners (Outlook Safe Links and others) open links before people do. A single-use
  sign-in link would be spent by the scanner, and a feedback link that records on GET would
  record verdicts nobody gave.
- Server-side sessions over signed JWTs: revocation is a row delete, and the database is
  already on every request path.
- "At most once" over "at least once": a missed email costs less than a duplicate, in the
  same way that a late alert costs less than a false one.
- Starting from the archive's newest pass rather than the clock alerts on passes that
  arrive in the catalog late, and never on what the user could already see.

### What would change our mind

Users wanting a password or SSO, abuse of the login endpoint (add rate limiting first), or
a deployment where the API and the web app are not same-site, which the SameSite=Lax cookie
assumes.

---

## ADR-013 — Map tiles go through the API; TiTiler is never exposed

**Status:** accepted · **Date:** 2026-11-08

### Context

TiTiler renders whatever file its `url=` parameter names. With the browser talking to it
directly, anyone who could reach it could read any area's composites, and an area around
someone's home is personal data (CLAUDE.md). Phase 9 left this open until deployment.

### Decision

The web app asks the API for tiles and point values
(`/aois/{id}/months/{YYYY-MM}/tiles/...`, `/point/...`). The API checks the session and the
area's owner, builds TiTiler's URL itself, and relays the answer. TiTiler has no public
port: in development it listens on 127.0.0.1 only, for an API running on the same machine,
and in deployment it isn't published at all.

### Reasoning

- Signed URLs would need code inside TiTiler to check them. A reverse-proxy `auth_request`
  would put the ownership rule in a second place. A relay keeps it in the one check every
  other AOI endpoint already uses.
- A caller can't choose the file: nothing in the request reaches `url=`.
- The cost is a thread and two primary-key lookups per tile, and those tiles are served
  `private, immutable`, so a browser asks for each one once. The database connection is
  handed back before TiTiler is called, since a map asks for many tiles at once.

### What would change our mind

Many concurrent users making the API the bottleneck for tiles: signed URLs checked at the
proxy, or pre-rendered frames per AOI-month (ADR-007's alternative).

---

## ADR-014 — On one machine, rasters stay on its disk; object storage is a nightly mirror

**Status:** accepted · **Date:** 2026-11-08 · **Changes:** ADR-006

### Context

ADR-006 put each AOI's Zarr cube and monthly COGs in object storage. Everything since has
read them from `data/` on a shared disk: the pipeline, TiTiler, and every worker. The
deployment direction is one machine (implementation plan, Phase 11).

### Decision

- On the single machine, rasters stay on local disk, as they do in development.
- Object storage, when it's configured, is a nightly `rclone sync` mirror of `cubes`,
  `masks` and `composites`, next to the Postgres dumps.
- The Postgres dumps are the backup that matters. Rasters can be rebuilt from STAC.

### Why

- Reading Zarr and COGs from object storage needs `s3fs` or `obstore` (a new dependency), a
  credentialled code path in the fetch, composite and tile steps, and a TiTiler that signs
  its reads. All of that would buy nothing on one machine, where the disk is faster and has
  no egress bill.
- 10 km² areas are small: Grünheide's ten years, with every pass and composite, is under
  500 MB, so a 40 GB disk holds dozens of areas.

### Consequences

- The machine's disk limits how many areas it can hold, and a second worker machine would
  need shared storage. That's the moment to implement ADR-006 as first written.
- `rclone sync` is a mirror, not a history: a raster deleted here is deleted there.

---

## ADR-015 — Deleting an area or an account deletes everything about it

**Status:** accepted · **Date:** 2026-11-10

### Context

An area around someone's home is personal data, and so are the pictures of it, the changes
found in it and the address alerts go to. Until now nothing could be removed except by hand
in the database.

### Decision

- `DELETE /aois/{id}` removes the area's row, and with it (every foreign key cascades) its
  passes, months, changes, alert rule, notifications, job runs and feedback. It then removes
  the area's cube, masks, composites and reports from disk.
- `DELETE /auth/me` does the same for every area the account owns, and removes the account,
  its sessions and its sign-in links.
- A backfill queued or running for a deleted area isn't cancelled through the queue. It finds
  the row gone at its next progress write, stops reading, and removes what it wrote since.
- Both are final. The web app asks once more before either, and says what goes.

### Reasoning

- **Feedback goes too.** "Not a real change" is training data, and a deleted area's feedback
  carries the footprint of a change inside it. Keeping it would keep part of the place. The
  retraining set shrinks; that is the price of the promise.
- **The worker checks the row, not the queue.** One rule covers a queued job, a running one
  and a retry, and the API needs no handle on the queue to delete.
- **Evaluation labels stay.** A file an admin saved to `eval/` is in git on purpose: it is the
  evaluation set, not a user's data.

### Consequences

- Nightly Postgres dumps still hold a deleted area for as long as they are kept
  (`ORBITDIFF_BACKUP_KEEP_DAYS`, 14 by default). The raster mirror follows the disk on its
  next sync.
- The STAC search cache keeps its per-year item lists under a hash of the geometry. It holds
  public catalog records and no polygon.

### What would change our mind

A legal need to keep records of alerts sent, which would mean keeping a minimal,
geometry-free row per notification.
