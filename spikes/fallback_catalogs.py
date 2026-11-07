"""Spike 4: what the fallback catalogs serve for the same AOI and month — asset format,
href scheme, and whether reading needs credentials.

Run: uv run python spikes/fallback_catalogs.py
"""

import json
import time
import urllib.request

import numpy as np
import rasterio
from pystac_client import Client
from rasterio.warp import transform_bounds
from rasterio.windows import from_bounds

AOI_BBOX = [30.674, 50.3845, 30.701, 50.396]  # the mockup's area
MONTH = "2025-08-01/2025-08-31"
CATALOGS = {
    "cdse": ("https://stac.dataspace.copernicus.eu/v1", "sentinel-2-l2a"),
    "planetary-computer": ("https://planetarycomputer.microsoft.com/api/stac/v1", "sentinel-2-l2a"),
}
WANTED = ("B04", "red", "B08", "nir", "SCL", "scl")

for name, (url, collection) in CATALOGS.items():
    print(f"\n=== {name}: {url} [{collection}]")
    try:
        items = list(
            Client.open(url)
            .search(collections=[collection], bbox=AOI_BBOX, datetime=MONTH, max_items=50)
            .items()
        )
    except Exception as exc:  # a spike: report and move on
        print("  search failed:", repr(exc))
        continue
    print(f"  {len(items)} items in {MONTH}")
    if not items:
        continue
    item = items[0]
    print("  id:", item.id)
    print(
        "  baseline:",
        item.properties.get("s2:processing_baseline") or item.properties.get("processing:version"),
    )
    print("  asset keys:", sorted(item.assets)[:40])
    for key, asset in sorted(item.assets.items()):
        if key.startswith(WANTED):
            print(f"  {key:12s} type={asset.media_type} href={asset.href[:110]}")
            alternates = asset.extra_fields.get("alternate")
            if alternates:
                print(
                    f"  {'':12s} alternate: { {k: v.get('href', '')[:90] for k, v in alternates.items()} }"
                )


# Same pass from Planetary Computer and Earth Search: does PC need more than an anonymous
# SAS token, and does its red band still carry the +1000 offset that Earth Search removes?
def red_median(href: str) -> tuple[float, float]:
    t0 = time.perf_counter()
    with rasterio.Env(GDAL_DISABLE_READDIR_ON_OPEN="EMPTY_DIR"), rasterio.open(href) as src:
        bounds = transform_bounds("EPSG:4326", src.crs, *AOI_BBOX)
        data = src.read(
            1, window=from_bounds(*bounds, transform=src.transform).round_offsets().round_lengths()
        )
    return float(np.median(data[data > 0])), time.perf_counter() - t0


PASS = "2025-08-31"
token_url = "https://planetarycomputer.microsoft.com/api/sas/v1/token/sentinel-2-l2a"
with urllib.request.urlopen(token_url) as r:
    token = json.load(r)["token"]
pc_item = next(
    i
    for i in Client.open(CATALOGS["planetary-computer"][0])
    .search(collections=["sentinel-2-l2a"], bbox=AOI_BBOX, datetime=PASS)
    .items()
    if "T36UUA" in i.id
)
es_item = next(
    i
    for i in Client.open("https://earth-search.aws.element84.com/v1")
    .search(collections=["sentinel-2-l2a"], bbox=AOI_BBOX, datetime=PASS)
    .items()
    if "36UUA" in i.id
)
print(f"\n=== same pass {PASS}, tile 36UUA, red band over the AOI")
for name, href in [
    ("planetary-computer", f"{pc_item.assets['B04'].href}?{token}"),
    ("earth-search l2a", es_item.assets["red"].href),
]:
    median, seconds = red_median(href)
    print(f"  {name:20s} median DN {median:.0f}  ({seconds:.1f}s)")
print(
    f"  earth-search boa_offset_applied={es_item.properties.get('earthsearch:boa_offset_applied')}"
)
