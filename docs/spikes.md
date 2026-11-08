# Phase 0 spikes: findings

Run on 2026-11-07 from the development machine, against the mockup's area (bbox
`30.674, 50.3845, 30.701, 50.396`, about 1.9 × 1.3 km near Kyiv). Scripts are in
`spikes/`; re-run them before relying on a number that matters. The decisions these
findings feed are in `docs/ADR.md`.

## 1. Archive coverage on Earth Search

`spikes/coverage_and_offset.py`, period 2017-01-01 to 2026-11-06.

| Collection | Items | Years covered | Processing baselines |
|---|---|---|---|
| `sentinel-2-l2a` | 3,741 | 2017–2026, every year | Mixed: 00.01 (2017–18), 02.07–03.01, 04.00 (2022), 05.00–05.13 |
| `sentinel-2-c1-l2a` | 2,273 | 2018-03 onward; **no 2017, only 26 items in 2022** | Uniform: 05.00 and later |
| `sentinel-2-pre-c1-l2a` | 0 | — | — |

Items per year, `sentinel-2-l2a`: 2017: 82 · 2018: 330 · 2019: 512 · 2020: 586 ·
2021: 592 · 2022: 296 · 2023: 292 · 2024: 294 · 2025: 404 · 2026: 353.

What else showed up:

- **Each pass arrives up to four times.** The 3,741 items are 1,402 distinct
  (day, platform) passes. Two causes stack:
  - The AOI sits in the overlap of **two MGRS tiles in different UTM zones**:
    `35UQR` (EPSG:32635) and `36UUA` (EPSG:32636).
  - For 2018-03 to 2021-12, `sentinel-2-l2a` holds both the original processing
    and ESA's 05.00 reprocessing of the same acquisition (for example
    `S2B_36UUA_20210815_0_L2A` and `S2B_36UUA_20210815_1_L2A`).
- **2017 comes from a different processor.** Its baseline reads `00.01`, not an ESA
  baseline. ESA only started producing L2A systematically in 2018. Expect a
  radiometric step at the 2017/2018 boundary.
- **About 143 passes per year** reach this AOI, about twice the nominal 5-day
  revisit, because it lies where two relative orbits overlap.
- The tile code property is inconsistent: most items carry `grid:code`
  (`MGRS-36UUA`), a few only `s2:mgrs_tile` (`36UUA`).
- **STAC search over the full archive is slow.** Paging ~6,000 items from Earth Search
  took about 6 minutes. Phase 1 should search year by year in parallel and cache the
  results.

## 2. The reflectance offset: metadata vs pixels

The −1000 offset introduced with processing baseline 04.00 is described three different
ways, and the item metadata on Earth Search can't be taken at face value.

**Metadata** (`spikes/coverage_and_offset.py`):

| Collection | Baselines | `earthsearch:boa_offset_applied` | `raster:bands` offset (red) |
|---|---|---|---|
| `sentinel-2-l2a` | before 04.00 | `False` | 0 |
| `sentinel-2-l2a` | 04.00, 05.10 | `False` | −0.1 |
| `sentinel-2-l2a` | 05.00, 05.09, 05.11–05.13 | `True` | −0.1 |
| `sentinel-2-c1-l2a` | 05.00 and later | absent | −0.1 |

**Pixels** (`spikes/read_cost_and_offset_check.py`). One clear pass (2021-08-15, tile
36UUA) in three versions, red band median DN over the AOI:

| Version | Red median | NIR median |
|---|---|---|
| `sentinel-2-l2a`, baseline 03.01 | 665 | 2256 |
| `sentinel-2-l2a`, baseline 05.00, `boa_offset_applied=True` | 678 | 2304 |
| `sentinel-2-c1-l2a`, baseline 05.00 | 1678 | 3304 |

And a recent pass (2025-08-31, tile 36UUA, baseline 05.11), from `spikes/fallback_catalogs.py`:

| Catalog | Red median |
|---|---|
| Earth Search `sentinel-2-l2a` (`boa_offset_applied=True`) | 734 |
| Planetary Computer `sentinel-2-l2a` | 1734 |

**Conclusion:**

- `boa_offset_applied=True` means **Earth Search already removed the offset** from the
  pixels.
- Their `raster:bands` offset of −0.1 is therefore **wrong for those items**. Applying
  it would push reflectance down by 0.1 (about 1000 DN), which is a fake change of exactly
  the kind this project exists to avoid.
- The `c1` collection and Planetary Computer keep the offset in the data.

The harmonization rule that fits every case seen:

```
offset_dn = -1000  if baseline >= "04.00" and not boa_offset_applied  else 0
reflectance = (DN + offset_dn) / 10000
```

Ignore `raster:bands` offsets. Test fixtures should come from the three versions above.

Two smaller observations:

- Baseline 03.01 and 05.00 differ by ~2% in red and NIR for the same pass, so mixing
  baselines leaves a small systematic step. Detection thresholds must sit well above it.
- SCL differs between baselines for the same pass. The 03.01 version flagged a few
  pixels as cloud (classes 8, 9) and unclassified (7); 05.00 flagged none.

## 3. What a windowed read costs

`spikes/read_cost_and_offset_check.py`. These are cold, sequential, single-threaded reads
from this machine.

| Band | Internal block | Blocks touched | Bytes read | Full file | Share | Wall time |
|---|---|---|---|---|---|---|
| red (10 m) | 1024 × 1024 | 1 | 1.46 MB | 212 MB | 0.7% | 5–13 s |
| nir (10 m) | 1024 × 1024 | 1 | 1.63 MB | 243 MB | 0.7% | 6–8 s |
| scl (20 m) | 512 × 512 | 1 | 0.03 MB | 2.3 MB | 1.3% | ~2.6 s |

- **Bytes are quantized by the block, not the AOI.** One 10 m block covers 10.24 km, so
  any AOI that fits inside one block costs the same ~1.5 MB per band. An AOI that
  straddles block edges costs 2–4 blocks. Fetch cost therefore barely limits AOI size up
  to the 10 km² cap; compute and cache size do.
- **Per pass:** four 10 m bands plus SCL is about 6 MB, against ~850 MB for the full
  assets.
- **Per AOI backfill:** ~1,400 passes × 6 MB ≈ 8–9 GB, if every pass is fetched.
- **Latency dominates.** Even the 30 KB SCL read took ~2.6 s from Earth Search
  (`us-west-2`). The same band window from Planetary Computer (Azure) took **0.7 s vs
  10.3 s**. Sequentially, one AOI's backfill would take hours, so Phase 1 needs
  concurrent reads.

Implications for Phase 1:

- **Read SCL first.** It's 1/50th of the bytes. Fetch the 10 m bands only for passes with
  clear pixels over the AOI. With this AOI's cloud climate, that should cut bytes and time
  roughly in half.
- **Read each pass once.** Prefer the tile in the AOI's own UTM zone and use the other
  tile only when the first has no data over the AOI. Take the newest processing
  baseline when a pass exists twice.
- **Read concurrently.** Use a thread pool of 16–32 readers per backfill.

## 4. Fallback catalogs

`spikes/fallback_catalogs.py`, one month (2025-08) over the AOI. Both found 38 items.

| | Copernicus Data Space (CDSE) | Microsoft Planetary Computer |
|---|---|---|
| STAC | `stac.dataspace.copernicus.eu/v1`, `sentinel-2-l2a` | `planetarycomputer.microsoft.com/api/stac/v1`, `sentinel-2-l2a` |
| Asset format | **JPEG 2000** (`image/jp2`) | **COG** |
| Asset hrefs | `s3://eodata/...` (needs CDSE S3 credentials); HTTPS via the OData "zipper" | `https://sentinel2l2a01.blob.core.windows.net/...` + free anonymous SAS token |
| Asset keys | `B04_10m`, `B08_10m`, `SCL_20m`, … | `B04`, `B08`, `SCL`, … |
| Offset in pixels | not measured (JP2 + credentials) | **kept** (+1000 DN for baseline ≥ 04.00) |
| Search speed | fast | fast (both searches together < 4 s) |

CDSE serves the original SAFE JPEG 2000 files. Windowed reads over JP2 are much less
efficient than over COGs, and they need credentials. Planetary Computer serves the same
product as COGs, needs only an anonymous token, and was much faster from this machine.
Proposal in ADR-002.

## 5. A late-ingested item whose offset flag is wrong (found 2026-11-07)

During the Phase 4 smoke run (spike AOI, Jul–Nov 2026, 75 passes), one Earth Search item
disagreed with its neighbours: `S2A_36UUA_20260707_0_L2A`, baseline 05.12, ingested on
17 Aug 2026 (six weeks after the pass) by `sentinel2-to-stac` 2026.08.16, with
`earthsearch:boa_offset_applied: false`. Every other item in the run says `true`.

Its pixels say otherwise. Tile-wide red DN at 1/16 resolution, over pixels that SCL calls
vegetation or bare soil on both days:

| Reference pass | Pixels | Late item median | Reference median | Median difference |
|---|---|---|---|---|
| `S2B_36UUA_20260713_0_L2A` | 5,918 | 862 | 776 | +104 DN |
| `S2B_36UUA_20260703_0_L2A` | 61,917 | 651 | 490 | +95 DN |

An offset still in the pixels would show about +1000 DN. These differences are ordinary
day-to-day and platform variation, so the offset is already removed and the flag is wrong.
Following it (ADR-003) would subtract 1000 a second time and push red reflectance below
zero. Here the pass was too cloudy over the AOI for its bands to be read, so no composite
used it.

Script: `spikes/offset_flag_check.py <suspect item> <reference item>` reads each item's red
and SCL overviews (under 1 MB each).

### Follow-up: how often, and a per-pass check

**The flag is wrong on a run of 2022 items and right on others.** Over the spike AOI,
Earth Search holds 26 tile-36UUA items from 2022 at baseline 04.00 or later without
`boa_offset_applied: true`. Dark targets settle each one: the 2nd percentile of
min(blue, red) DN over pixels SCL calls vegetation, bare soil or water, tile-wide at 1/16
resolution (`spikes/offset_dark_targets.py`). Clear land and water always have some pixels
at 0–0.07 reflectance, so a few hundred DN means no offset and 1000+ DN means the offset
is there. The items split by date:

| Unflagged passes, tile 36UUA | Items | Judged (enough pixels) | Dark p2 | Offset in the pixels? |
|---|---|---|---|---|
| 25 Jan – 26 Feb 2022 | 12 | 6 | 1002–1369 DN | yes: the flag is right |
| 18 Mar – 3 Nov 2022 | 14 | 11 | 3–366 DN | no: the flag is wrong |
| `S2A_36UUA_20260707_0_L2A` (above) | 1 | 1 | 206 DN | no |

For comparison, items flagged `true` that we sampled read 120–213 DN, and the `c1` item
`S2B_T36UUA_20210815T085845_L2A` (offset in the pixels) reads 1161 DN. Planetary Computer,
which keeps the offset, agrees pass by pass (same pixels, `--pc`):

| Earth Search item (unflagged) | Earth Search dark p2 | Planetary Computer dark p2 |
|---|---|---|
| `S2B_36UUA_20220214_0_L2A` | 1074 | 1074 |
| `S2A_36UUA_20220226_0_L2A` | 1108 | 1070 |
| `S2A_36UUA_20220318_0_L2A` | 177 | 1147 |
| `S2B_36UUA_20220601_0_L2A` | 141 | 1143 |

So no metadata rule fits: "Earth Search never carries the offset" is wrong for February
2022, and following the flag is wrong from March. A date window learned from one tile is a
guess. Each pass is judged by its own pixels instead.

**The check** (`masking.offset_suspect`, settings under `[masking]` in
`config/pipeline.toml`): the same dark-target statistic, in reflectance after harmonizing,
over the AOI window. Below −0.02 the offset was subtracted from pixels that didn't carry it.
The pass gets no clear pixels, so no composite uses it, and it's recorded as
`scenes.offset_suspect`. With fewer than 100 reference pixels the metadata is trusted.

Calibration and validation over AOI windows, after harmonizing:

| Passes | Count | Dark p2 (reflectance) | Flagged suspect |
|---|---|---|---|
| Correct: local cubes (spike AOI Jul–Nov 2026, Kyiv AOI Jun–Sep 2021) | 73 | 0.006 – 0.067 | 0 |
| Correct: Kyiv AOI Jan–Aug 2022, 3 of them with the offset in the pixels | 31 | 0.002 – 0.133 | 0 |
| Flag wrong: Kyiv AOI Mar–Aug 2022 | 7 | −0.085 – −0.048 | 7 |

Without the check, the 7 would have reached composites. March and April 2022 hold 4 of
them. Their composites would have lost about 0.05 of red reflectance and gained +0.18 and
+0.25 NDVI (median), with NDVI moving more than 0.1 over 72% and 87% of the AOI. That's a
fake greening against the same months of any other year. Summer months, with more good
passes to outvote one bad pass, moved by 0.002–0.004 in red.

**Limits:**

- Only an offset subtracted twice is caught. An offset left in looks like a bright
  surface (one correct March pass reads 0.133), so that direction isn't checked. A new
  catalog still needs its pixels checked against a known pass (ADR-003).
- Over a bright AOI (desert, dense city) a wrongly flagged pass can read above −0.02 and
  get through. Comparing each pass with its neighbours in the same AOI would catch that;
  it isn't needed for any AOI so far.
- A pass with fewer than 100 reference pixels in the AOI keeps its metadata.

## 6. The building model: trained on L1C, served on L2A (2026-11-07)

The building model (Phase 8) trains on OSCD, which is L1C (top-of-atmosphere
reflectance), and serves on our L2A composites (surface reflectance). Each image is
standardised per band, over the pixels seen on both dates, before the model sees it.
`spikes/buildings_skew.py --version 1` compares OSCD's 10 test pairs with Grünheide's
107 anniversary pairs (area 3, 2017–2026). 15 of those pairs share too few clear pixels
to compare, and the model skips them too.

**Raw reflectance is far apart.** Median per band, every pixel of both dates:

| Band | OSCD (L1C) | Grünheide (L2A) |
|---|---|---|
| blue | 0.139 | 0.032 |
| green | 0.130 | 0.045 |
| red | 0.137 | 0.036 |
| nir | 0.232 | 0.210 |

Part of the gap is the atmosphere: path radiance brightens the visible bands most. Part
is land cover: OSCD is cities, and Grünheide is mostly pine forest. Either way, a model
fed raw reflectance would see inputs unlike anything it trained on.

**Standardised, the inputs line up.** This is the spread (standard deviation) of the
after-minus-before difference the model sees, as the median over pairs:

| Band | OSCD | Grünheide |
|---|---|---|
| blue | 0.62 | 0.53 |
| green | 0.60 | 0.53 |
| red | 0.57 | 0.53 |
| nir | 0.65 | 0.70 |

**What the model flags**, at the threshold of 0.5:
- On OSCD's test cities, 1.95% of unchanged pixels (false positives) and 33.2% of changed
  ones.
- On Grünheide, by the year of the later month:

| Year | 2018 | 2019 | 2020 | 2021 | 2022 | 2023 | 2024 | 2025 | 2026 |
|---|---|---|---|---|---|---|---|---|---|
| Pixels flagged | 0.5% | 0.8% | 7.3% | 8.8% | 7.0% | 5.6% | 2.1% | 2.4% | 1.9% |

Before construction, the model flags fewer Grünheide pixels than its false-positive rate
on OSCD, so L2A inputs don't make it see change everywhere. From 2020 to 2023 the Tesla
factory goes up inside the area: the flags rise about tenfold, then fall back.

All of this is qualitative: no labels say which flagged pixels are right. The model's
event precision and recall on L2A come from the evaluation set, once it is verified.

### Measured directly: one city, both levels (2026-11-08)

OSCD has one test city with both dates inside the L2A archive: Chongqing, 2017-04-14 and
2018-04-02. `spikes/buildings_l2a_gap.py --version 1` scores the model against OSCD's labels
on OSCD's own L1C, and on the L2A passes of the same tile and days, read from Earth Search
onto OSCD's grid. Both are scored on the same pixels: the 94.2% clear on both dates by SCL,
24,641 of them changed.

| Input | F1 | Precision | Recall | Pixels flagged |
|---|---|---|---|---|
| L1C (what it trained on) | 0.246 | 0.705 | 0.149 | 1.39% |
| L2A (what it serves on) | 0.252 | 0.696 | 0.154 | 1.45% |

The model scores the same on either. The two agree on 99.59% of pixels, and of the pixels
either one flags, both flag 74.8%. So on this city the L1C-to-L2A step costs nothing
measurable, and the raw gap (blue 0.125 against 0.073 here) is taken out by standardising.

It is one city and one pair, and it's a city, like the training data. It says the input
level isn't the problem. It says nothing about how the model does on forest and farmland,
which only the evaluation set can.

## 7. Feedback as hard negatives: a first look (2026-11-08)

There is no real feedback yet, so Grünheide's 146 usable `dismissed` events stood in for
"Not a real change": 67 from before 2023 to train on, 79 from 2023 on held out. Two full
training runs, same seed, same OSCD crops. Nothing was registered.

| Training | Validation F1 | OSCD test F1 | Precision | Recall | Held-out false alarms |
|---|---|---|---|---|---|
| OSCD only | 0.437 | 0.403 | 0.444 | 0.369 | 20.1% |
| OSCD + 67 hard negatives | 0.415 | 0.313 | 0.413 | 0.252 | 4.1% |

"Held-out false alarms" is the share of held-out "not real" pixels the model still calls
changed.

**It learns what it's told, and it pays for it.** False alarms on the held-out events fall
fivefold. Recall on OSCD's test cities falls by a third, and precision there doesn't improve.
At 4 of 16 crops per batch the model has become more reluctant to call anything a change.
A candidate like this should fail the promotion gate, which allows a recall loss of 0.02.

Don't read much more into it:

- Dismissed events aren't people's feedback: a pass that didn't confirm a change is a
  different thing from someone looking at it and saying no.
- With one area the split had to be by time, so the held-out pixels are in the place the
  model trained on. The product splits by area, and the 4.1% is likely flattering.
- `feedback_per_batch = 4` is a first guess. The right share should be chosen on the verified
  evaluation set, never on OSCD's test cities.

**Reproducibility.** The first row isn't version 1's numbers (validation 0.416, test 0.392).
The seed and crops are the same, but the loss is now averaged per patch, which is the same
maths with different floating-point rounding. Over 3,000 steps that moved F1 by about 0.02, so
differences of that size between two runs are noise.

## 8. Events against Hansen Global Forest Change (2026-11-08)

`orbitdiff evaluate` now compares `vegetation_loss` events still standing (pending or confirmed)
with the loss years in GFC-2025-v1.13, and reports GFC loss that no event covers. **This is a
second opinion, not ground truth.** GFC is another model's annual map from Landsat. Its own notes
call it a relative indicator and warn against taking definitive areas from its pixel counts. It
can only speak for stand-replacing loss of trees that stood in 2000, and its last year is 2025.
None of this feeds detection, alerts or the promotion gate.

The run covered Grünheide (8.52 km², January 2017 to November 2026), from the cube and composites
already on disk, so no imagery was refetched. `eval/` has no labelled areas yet, so the run used a
throwaway copy of the polygon with no labels, and its labelled table measures nothing. GFC years
judged: 2017 to 2025. There are 45 GFC patches above the minimum mapping unit in those years.

| Method | Vegetation-loss events | Matched within 1 yr | No GFC loss | Not forest in 2000 | After 2025 | Patches seen as vegetation loss | Seen as another category | Seen by no event |
|---|---|---|---|---|---|---|---|---|
| naive | 100 | 32 | 12 | 26 | 30 | 21 | 18 | 6 |
| cva | 97 | 58 | 5 | 11 | 23 | 13 | 27 | 5 |
| bfast | 28 | 9 | 5 | 9 | 5 | 8 | 36 | 1 |
| production | 20 | 14 | 1 | 4 | 1 | 11 | 12 | 22 |

"Production, confirmed only" is identical, because no event is still pending. An event matches
when GFC loss in the same or an adjacent year covers at least a quarter of it. A patch counts as
seen when standing events from those years cover at least a quarter of it.

**What production keeps, GFC mostly corroborates.** 14 of the 15 events it can judge have GFC
loss under them. The Tesla clearing (90.24 ha, March 2020) is 95% GFC loss in 2019 to 2021. The
timing carries information: with the same events moved 3, 4 or 5 years either way, 0 to 3 of them
match instead of 14. Three of the 14 rest on less than half their footprint, though. The weakest
(5.35 ha, January 2023) is mostly on loss GFC dates to 2020.

**The category is a bigger gap than detection.** 12 patches, 127 ha including 69 ha in 2021 and
32 ha in 2023, are covered only by events of another category. Forest cleared down to bare ground
or roofs isn't being called `vegetation_loss`.

**"Seen by no event" turns out to mean "filed as seasonal".** 22 patches, 20.2 ha in all, median
0.42 ha, 12 of them under the default 0.5 ha alert size. Every one lies at least a quarter under
an event the pipeline labelled `seasonal` in some year, and 1,225 of production's 1,604 events
here are seasonal. Either the seasonal rule is too eager on real clearings, or these are GFC's
errors. One area can't say which. No threshold was changed; it's something to check while
verifying labels.

The rest don't say much. The one event with no GFC loss (0.28 ha, January 2023) sits on loss GFC
dates to 2021, two years earlier. The four "not forest" events have too little canopy in the 2000
layer for GFC to speak for them, which covers trees that grew in since.

**Limits.** One area, mostly pine, with one very large clearing. A 3-year window is generous. The
seen and unseen counts inherit detection's 3 x 3 opening, on purpose, so that both sides mean the
same thing by a region. Disagreement here is a place to look, never a false alert or a miss.

**Cost.** 1.21 MB of 411 MB for both layers. GFC's GeoTIFFs are striped with no overviews, so
they're not COGs: a window costs whole 10-degree rows, but only the 100 or so it crosses of
40,000. Bytes are counted as everywhere else, from the blocks under the window. The TIFF header
and strip tables aren't in that figure; GDAL logged about 0.2 MB of separate range requests per
layer. Layers are kept per grid under `data/gfc/<version>/`, so changing a threshold reads nothing
again.

Source: Hansen/UMD/Google/USGS/NASA (CC BY 4.0; citation in the README).
