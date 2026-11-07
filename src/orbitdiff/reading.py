"""Windowed COG reads onto an AOI's grid, with the bytes they cost.

Each asset is opened over HTTP and warped onto the AOI grid. GDAL fetches only the COG
blocks under the window. SCL is read first because it costs ~1/50th of the 10 m bands:
when the AOI is clouded out, the 10 m bands are never requested (docs/spikes.md, 3).
"""

import math
from collections.abc import Callable, Collection, Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

import numpy as np
import rasterio
from loguru import logger
from rasterio.enums import Resampling
from rasterio.errors import RasterioIOError
from rasterio.vrt import WarpedVRT
from rasterio.warp import transform_bounds
from rasterio.windows import from_bounds

from orbitdiff.aoi import AOIGrid
from orbitdiff.catalog import SceneItem
from orbitdiff.config import BANDS

GDAL_ENV = {
    "GDAL_DISABLE_READDIR_ON_OPEN": "EMPTY_DIR",
    "GDAL_HTTP_MERGE_CONSECUTIVE_RANGES": "YES",
    "GDAL_HTTP_MULTIPLEX": "YES",
    "GDAL_HTTP_VERSION": "2",
    "GDAL_HTTP_MAX_RETRY": "4",
    "GDAL_HTTP_RETRY_DELAY": "1",
    "AWS_NO_SIGN_REQUEST": "YES",
    "VSI_CACHE": "TRUE",
}


@dataclass(frozen=True)
class BandRead:
    data: np.ndarray  # on the AOI grid, 0 = nodata
    bytes_read: int  # compressed bytes of the COG blocks under the window
    full_bytes: int  # compressed bytes of the whole full-resolution band


def _block_bytes(src: rasterio.DatasetReader, grid: AOIGrid) -> tuple[int, int]:
    bh, bw = src.block_shapes[0]
    n_rows, n_cols = math.ceil(src.height / bh), math.ceil(src.width / bw)
    w = from_bounds(
        *transform_bounds(grid.crs, src.crs, *grid.bounds, densify_pts=21), transform=src.transform
    )
    rows = range(
        max(0, math.floor(w.row_off) // bh), min(n_rows, math.ceil(w.row_off + w.height) // bh + 1)
    )
    cols = range(
        max(0, math.floor(w.col_off) // bw), min(n_cols, math.ceil(w.col_off + w.width) // bw + 1)
    )
    read = sum(src.block_size(1, i, j) for i in rows for j in cols)
    full = sum(src.block_size(1, i, j) for i in range(n_rows) for j in range(n_cols))
    return read, full


def read_band(href: str, grid: AOIGrid, categorical: bool = False) -> BandRead:
    """Read one single-band asset onto the AOI grid.

    Categorical rasters (SCL) always use nearest neighbour: interpolating class codes
    invents classes. Reflectance uses nearest on the asset's own grid and bilinear only
    when the asset is in another UTM zone and has to be reprojected.
    """
    with rasterio.Env(**GDAL_ENV), rasterio.open(href) as src:
        bytes_read, full_bytes = _block_bytes(src, grid)
        same_grid = src.crs == grid.crs
        resampling = Resampling.nearest if categorical or same_grid else Resampling.bilinear
        with WarpedVRT(
            src,
            crs=grid.crs,
            transform=grid.transform,
            width=grid.width,
            height=grid.height,
            resampling=resampling,
            src_nodata=src.nodata if src.nodata is not None else 0,
            nodata=0,
        ) as vrt:
            data = vrt.read(1)
    return BandRead(data, bytes_read, full_bytes)


def scl_clear_frac(scl: np.ndarray, aoi_mask: np.ndarray, scl_drop: Collection[int]) -> float:
    """Share of the AOI's pixels that SCL calls clear (no data counts as not clear)."""
    clear = aoi_mask & ~np.isin(scl, list(scl_drop))
    return float(clear.sum() / aoi_mask.sum())


@dataclass(frozen=True)
class PassRead:
    scene: SceneItem
    status: str  # "full": SCL + bands; "scl": too cloudy over the AOI; "tile_cloud": nothing read
    scl: np.ndarray | None
    bands: Mapping[str, np.ndarray] | None
    clear_frac: float  # from SCL, over the AOI; NaN when SCL wasn't read
    bytes_read: int
    full_bytes: int


def read_pass(
    candidates: list[SceneItem],
    grid: AOIGrid,
    *,
    scl_drop: Collection[int],
    fetch_min_clear_frac: float,
    max_tile_cloud_pct: float,
    sign: Callable[[SceneItem, str], str],
) -> PassRead | None:
    """Read one pass from its best readable candidate; None if every candidate failed."""
    best = candidates[0]
    if best.tile_cloud_pct > max_tile_cloud_pct:
        return PassRead(best, "tile_cloud", None, None, math.nan, 0, 0)

    for scene in candidates:
        try:
            scl = read_band(sign(scene, "scl"), grid, categorical=True)
            clear_frac = scl_clear_frac(scl.data, grid.mask, scl_drop)
            if clear_frac < fetch_min_clear_frac:
                return PassRead(
                    scene, "scl", scl.data, None, clear_frac, scl.bytes_read, scl.full_bytes
                )
            # Each read is latency-bound (seconds per band), so the bands go in parallel.
            with ThreadPoolExecutor(len(BANDS)) as pool:
                futures = {b: pool.submit(read_band, sign(scene, b), grid) for b in BANDS}
            reads = {band: future.result() for band, future in futures.items()}
        except RasterioIOError as exc:
            logger.warning("read failed for {}: {}", scene.item_id, exc)
            continue
        return PassRead(
            scene,
            "full",
            scl.data,
            {band: r.data for band, r in reads.items()},
            clear_frac,
            scl.bytes_read + sum(r.bytes_read for r in reads.values()),
            scl.full_bytes + sum(r.full_bytes for r in reads.values()),
        )
    return None
