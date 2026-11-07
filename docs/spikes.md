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

**Open question for ADR-003:** trust the flag less. Options: a pixel sanity check per pass
(e.g. a large share of negative harmonized reflectance over SCL vegetation/bare pixels marks
the pass suspect and keeps it out of composites), or treating late-ingested items by
processing software version once more cases are known.
