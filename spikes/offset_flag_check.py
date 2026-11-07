"""Does an Earth Search item's pixels carry the BOA offset its metadata claims? (spike 5)

Usage: uv run python spikes/offset_flag_check.py <suspect item id> <reference item id>
The reference should be a nearby pass whose offset handling is known to be right.
"""

import json
import sys
import urllib.request

import numpy as np
import rasterio
from rasterio.enums import Resampling

BASE = "https://earth-search.aws.element84.com/v1/collections/sentinel-2-l2a/items/"
OUT = (686, 686)  # 1/16 of a 10 m tile


def read(item_id: str) -> tuple[np.ndarray, np.ndarray]:
    item = json.load(urllib.request.urlopen(BASE + item_id))
    with rasterio.Env(GDAL_DISABLE_READDIR_ON_OPEN="EMPTY_DIR", AWS_NO_SIGN_REQUEST="YES"):
        with rasterio.open(item["assets"]["red"]["href"]) as src:
            red = src.read(1, out_shape=OUT, resampling=Resampling.nearest).astype(float)
        with rasterio.open(item["assets"]["scl"]["href"]) as src:
            scl = src.read(1, out_shape=OUT, resampling=Resampling.nearest)
    return red, scl


late, ref = read(sys.argv[1]), read(sys.argv[2])
both = np.isin(late[1], [4, 5]) & np.isin(ref[1], [4, 5]) & (late[0] > 0) & (ref[0] > 0)
diff = late[0][both] - ref[0][both]
print(f"pixels clear on both days: {both.sum()}")
print(
    f"red DN median: late-ingest {np.median(late[0][both]):.0f}, reference {np.median(ref[0][both]):.0f}"
)
print(f"median per-pixel difference: {np.median(diff):.0f} DN (offset in pixels would show ~+1000)")
