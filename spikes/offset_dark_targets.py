"""Do an Earth Search item's pixels carry the BOA offset? Dark targets say (spike 5).

Usage: uv run python spikes/offset_dark_targets.py [--pc] <item id> ...

For each `sentinel-2-l2a` item: the 2nd percentile of min(blue, red) DN over pixels SCL
calls vegetation, bare soil or water, from 1/16-resolution overviews (under 1 MB per
band). Clear land and water always have some pixels at 0-0.07 reflectance, so a few
hundred DN means no offset in the pixels and 1000+ DN means the offset is there.

With --pc, the same pass from Planetary Computer, which keeps the offset in its pixels,
is read too, over the pixels both call reference.
"""

import json
import sys
import urllib.request

import numpy as np
import rasterio
from pystac_client import Client
from rasterio.enums import Resampling

EARTH_SEARCH = "https://earth-search.aws.element84.com/v1"
PLANETARY = "https://planetarycomputer.microsoft.com/api/stac/v1"
PC_TOKEN = "https://planetarycomputer.microsoft.com/api/sas/v1/token/sentinel-2-l2a"
OUT = (686, 686)  # 1/16 of a 10 m tile
REFERENCE_SCL = [4, 5, 6]  # vegetation, bare soil, water


def read(hrefs: dict[str, str]) -> dict[str, np.ndarray]:
    out = {}
    with rasterio.Env(GDAL_DISABLE_READDIR_ON_OPEN="EMPTY_DIR", AWS_NO_SIGN_REQUEST="YES"):
        for band, href in hrefs.items():
            with rasterio.open(href) as src:
                out[band] = src.read(1, out_shape=OUT, resampling=Resampling.nearest)
    return out


def reference(x: dict[str, np.ndarray]) -> np.ndarray:
    return np.isin(x["scl"], REFERENCE_SCL) & (x["blue"] > 0) & (x["red"] > 0)


def dark(x: dict[str, np.ndarray], where: np.ndarray) -> float:
    values = np.minimum(x["blue"], x["red"])[where]
    return float(np.percentile(values, 2)) if values.size else float("nan")


compare = "--pc" in sys.argv
item_ids = [a for a in sys.argv[1:] if a != "--pc"]
es = Client.open(EARTH_SEARCH).get_collection("sentinel-2-l2a")
if compare:
    pc = Client.open(PLANETARY)
    token = json.load(urllib.request.urlopen(PC_TOKEN))["token"]

for item_id in item_ids:
    item = es.get_item(item_id)
    p = item.properties
    a = read({b: item.assets[b].href for b in ("blue", "red", "scl")})
    where = reference(a)
    line = (
        f"{item_id:26} baseline {p['s2:processing_baseline']} "
        f"flag {str(p.get('earthsearch:boa_offset_applied')):5} "
        f"created {p.get('created', '')[:10]} | px {where.sum():6} | dark p2 {dark(a, where):5.0f}"
    )
    if compare:
        twin = next(
            i
            for i in pc.search(
                collections=["sentinel-2-l2a"],
                intersects=item.geometry,
                datetime=item.datetime.strftime("%Y-%m-%d"),
            ).items()
            if i.properties["s2:mgrs_tile"] == item_id.split("_")[1]
        )
        b = read(
            {k: twin.assets[v].href + "?" + token for k, v in (("blue", "B02"), ("red", "B04"))}
            | {"scl": twin.assets["SCL"].href + "?" + token}
        )
        both = where & reference(b)
        line += f" | Planetary Computer {twin.properties['s2:processing_baseline']}: "
        line += f"px {both.sum()}, dark p2 Earth Search {dark(a, both):.0f} vs {dark(b, both):.0f}"
    print(line)
