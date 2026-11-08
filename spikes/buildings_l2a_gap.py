"""Spike 6, second part: the same city, the same two dates, as L1C and as L2A.

Chongqing is the one OSCD test city with both dates inside the L2A archive (2017-04-14 and
2018-04-02). OSCD's L1C images are scored against its change labels, then the L2A passes of
the same tile and days, read from Earth Search onto OSCD's grid. Both are scored over the
same pixels: those clear on both dates by SCL. A measurement only: nothing is tuned on it.

Run after `orbitdiff train-buildings`:

    uv run --group ml python spikes/buildings_l2a_gap.py --version 1
"""

import argparse
from datetime import date
from pathlib import Path

import mlflow
import numpy as np
import rasterio
from rasterio.enums import Resampling
from rasterio.vrt import WarpedVRT
from shapely.geometry import box

from orbitdiff import catalog, oscd, radiometry, siamese
from orbitdiff.config import BANDS, pipeline_config, settings

CITY = "chongqing"

parser = argparse.ArgumentParser()
parser.add_argument("--version", type=int, required=True, help="registered model version")
args = parser.parse_args()

pipeline, data_dir = pipeline_config(), settings().data_dir
cfg = pipeline.buildings
mlflow.set_tracking_uri(settings().mlflow_tracking_uri)
model = siamese.from_registry(cfg.registered_model, args.version)
city = oscd.load_city(data_dir / "oscd", CITY, "test")

images = data_dir / "oscd" / oscd.IMAGES / CITY
georeferenced = next(Path(images / "imgs_1").glob("*_B02.tif"))
tile = georeferenced.stem.split("_")[0].removeprefix("T")
with rasterio.open(georeferenced) as src:
    grid = {
        "crs": src.crs,
        "transform": src.transform,
        "width": src.width,
        "height": src.height,
    }
    area = box(*src.bounds)


def on_oscd_grid(href: str, resampling: Resampling) -> np.ndarray:
    with rasterio.open(href) as src, WarpedVRT(src, resampling=resampling, **grid) as vrt:
        return vrt.read(1)


def l2a(day: str) -> tuple[np.ndarray, np.ndarray]:
    """The day's reflectance (band, y, x), NaN where SCL drops the pixel, and its clear mask."""
    wanted = date.fromisoformat(f"{day[:4]}-{day[4:6]}-{day[6:]}")
    found = catalog.search([pipeline.catalog.primary], area, wanted, wanted)
    scene = max((s for s in found if s.tile == tile), key=lambda s: s.baseline)
    print(f"{day}: {scene.item_id}, baseline {scene.baseline}, offset {scene.offset_dn}")
    scl = on_oscd_grid(scene.hrefs["scl"], Resampling.nearest)
    clear = (scl != 0) & ~np.isin(scl, sorted(pipeline.masking.scl_drop))
    bands = np.stack(
        [
            radiometry.to_reflectance(
                on_oscd_grid(scene.hrefs[band], Resampling.bilinear), scene.offset_dn
            )
            for band in BANDS
        ]
    )
    return np.where(clear, bands, np.nan).astype(np.float32), clear


def report(name: str, before: np.ndarray, after: np.ndarray, pixels: np.ndarray) -> np.ndarray:
    probability = model(before, after)
    found = probability > cfg.threshold
    scores = siamese.Scores(
        int((found & city.changed & pixels).sum()),
        int((found & ~city.changed & pixels).sum()),
        int((~found & city.changed & pixels).sum()),
    )
    print(
        f"{name}: F1 {scores.f1:.3f}, precision {scores.precision:.3f}, "
        f"recall {scores.recall:.3f}, flagged {found[pixels].mean():.2%}"
    )
    return found


l2a_before, clear_before = l2a(city.dates[0])
l2a_after, clear_after = l2a(city.dates[1])
pixels = clear_before & clear_after
print(f"Clear on both dates: {pixels.mean():.1%} of {pixels.size} pixels")
print(f"Changed pixels among them: {int(city.changed[pixels].sum())}")

for band, i in zip(BANDS, range(len(BANDS)), strict=True):
    print(
        f"   {band:6} L1C {np.median(city.before[i][pixels]):.3f}   "
        f"L2A {np.nanmedian(l2a_before[i][pixels]):.3f}"
    )
masked = [np.where(pixels, image, np.nan).astype(np.float32) for image in (city.before, city.after)]
l1c_found = report("L1C (what it trained on)", masked[0], masked[1], pixels)
l2a_found = report(
    "L2A (what it serves on) ",
    np.where(pixels, l2a_before, np.nan).astype(np.float32),
    np.where(pixels, l2a_after, np.nan).astype(np.float32),
    pixels,
)
agree = (l1c_found == l2a_found)[pixels].mean()
either = (l1c_found | l2a_found)[pixels].sum()
print(
    f"The two agree on {agree:.2%} of pixels; of pixels either flags, both flag "
    f"{(l1c_found & l2a_found)[pixels].sum() / max(either, 1):.1%}"
)
