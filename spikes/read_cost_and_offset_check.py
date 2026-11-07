"""Spikes 2b and 3: check the reflectance offset against pixel values, and measure what
a windowed read over one small AOI actually costs.

Same pass, same tile, three processing versions: the original baseline 03.01 and the
05.00 reprocessing in `sentinel-2-l2a`, and the 05.00 item in `sentinel-2-c1-l2a`.
If the 05.00 values sit ~1000 DN above 03.01, the -1000 offset is still in the data and
`raster:bands offset=-0.1` is right; if they match, Earth Search removed it already.

Run: uv run python spikes/read_cost_and_offset_check.py
"""

import math
import time
import urllib.request
from collections import defaultdict

import numpy as np
import rasterio
from pystac_client import Client
from rasterio.warp import transform_bounds
from rasterio.windows import Window, from_bounds

EARTH_SEARCH = "https://earth-search.aws.element84.com/v1"
AOI_BBOX = [30.674, 50.3845, 30.701, 50.396]  # the mockup's area, ~1.9 x 1.3 km
TILE = "36UUA"  # the AOI's own UTM zone
SEARCH_PERIOD = "2021-06-01/2021-09-30"
GDAL_ENV = {
    "GDAL_DISABLE_READDIR_ON_OPEN": "EMPTY_DIR",
    "CPL_VSIL_CURL_ALLOWED_EXTENSIONS": ".tif",
    "GDAL_HTTP_MERGE_CONSECUTIVE_RANGES": "YES",
    "GDAL_HTTP_MULTIPLEX": "YES",
    "GDAL_HTTP_VERSION": "2",
    "AWS_NO_SIGN_REQUEST": "YES",
}


def tile_of(item) -> str:
    return (
        item.properties.get("s2:mgrs_tile") or item.properties.get("grid:code", "")
    ).removeprefix("MGRS-")


def content_length(href: str) -> int:
    with urllib.request.urlopen(urllib.request.Request(href, method="HEAD")) as r:
        return int(r.headers["Content-Length"])


def read_window(href: str) -> dict:
    t0 = time.perf_counter()
    with rasterio.open(href) as src:
        left, bottom, right, top = transform_bounds("EPSG:4326", src.crs, *AOI_BBOX)
        w = from_bounds(left, bottom, right, top, transform=src.transform)
        col0, row0 = math.floor(w.col_off), math.floor(w.row_off)
        window = Window(
            col0,
            row0,
            math.ceil(w.col_off + w.width) - col0,
            math.ceil(w.row_off + w.height) - row0,
        )
        data = src.read(1, window=window)
        bh, bw = src.block_shapes[0]
        blocks = [
            (i, j)
            for i in range(window.row_off // bh, (window.row_off + window.height - 1) // bh + 1)
            for j in range(window.col_off // bw, (window.col_off + window.width - 1) // bw + 1)
        ]
        block_bytes = sum(src.block_size(1, i, j) for i, j in blocks)
        info = {
            "crs": src.crs.to_string(),
            "res": src.res[0],
            "block": (bh, bw),
            "blocks_touched": len(blocks),
            "block_bytes": block_bytes,
            "window_px": data.shape,
            "nodata": src.nodata,
        }
    info["seconds"] = time.perf_counter() - t0
    info["data"] = data
    return info


client = Client.open(EARTH_SEARCH)
l2a = [
    i
    for i in client.search(
        collections=["sentinel-2-l2a"], bbox=AOI_BBOX, datetime=SEARCH_PERIOD
    ).items()
    if tile_of(i) == TILE
]
c1 = [
    i
    for i in client.search(
        collections=["sentinel-2-c1-l2a"], bbox=AOI_BBOX, datetime=SEARCH_PERIOD
    ).items()
    if tile_of(i) == TILE
]

by_day = defaultdict(dict)
for item in l2a:
    by_day[item.datetime.date()][("l2a", item.properties["s2:processing_baseline"])] = item
for item in c1:
    by_day[item.datetime.date()][("c1", item.properties["s2:processing_baseline"])] = item

# Clearest day that has all three versions.
candidates = [
    (min(v.properties.get("eo:cloud_cover", 100) for v in versions.values()), day, versions)
    for day, versions in by_day.items()
    if {("l2a", "03.01"), ("l2a", "05.00"), ("c1", "05.00")} <= versions.keys()
]
cloud, day, versions = min(candidates, key=lambda c: c[0])
print(f"day {day}, tile {TILE}, tile cloud {cloud:.1f}%, versions: {sorted(versions)}\n")

for key in [("l2a", "03.01"), ("l2a", "05.00"), ("c1", "05.00")]:
    item = versions[key]
    print(
        f"--- {key}  id={item.id}  boa_offset_applied={item.properties.get('earthsearch:boa_offset_applied')}"
    )
    for band in ["red", "nir", "scl"]:
        href = item.assets[band].href
        r = read_window(href)
        full = content_length(href)
        valid = r["data"][r["data"] > 0]
        stats = (
            f"p5={np.percentile(valid, 5):.0f} median={np.median(valid):.0f}"
            if band != "scl"
            else f"classes={dict(zip(*np.unique(r['data'], return_counts=True), strict=True))}"
        )
        print(
            f"  {band:4s} {r['crs']} {r['res']:.0f} m block={r['block']} window={r['window_px']} "
            f"blocks={r['blocks_touched']} read={r['block_bytes'] / 1e6:.2f} MB of {full / 1e6:.1f} MB "
            f"({r['block_bytes'] / full:.1%}) in {r['seconds']:.1f}s | {stats}"
        )
    print(f"  href: {item.assets['red'].href}")
