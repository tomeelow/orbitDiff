"""Synthetic rasters for tests: small tiled GeoTIFFs that read like COGs."""

from pathlib import Path

import numpy as np
import rasterio
from rasterio.transform import Affine

from orbitdiff.aoi import AOIGrid


def write_tif(path: Path, data: np.ndarray, epsg: int, transform: Affine, block: int = 64) -> str:
    profile = {
        "driver": "GTiff",
        "width": data.shape[1],
        "height": data.shape[0],
        "count": 1,
        "dtype": data.dtype,
        "crs": f"EPSG:{epsg}",
        "transform": transform,
        "nodata": 0,
        "tiled": True,
        "blockxsize": block,
        "blockysize": block,
        "compress": "deflate",
    }
    with rasterio.open(path, "w", **profile) as dst:
        dst.write(data, 1)
    return str(path)


MARGIN_M = 1280  # synthetic tiles extend this far beyond the AOI grid on every side


def tile_for(grid: AOIGrid, res: int) -> tuple[Affine, tuple[int, int]]:
    """Transform and shape of a synthetic tile in the AOI's own zone, aligned to its grid."""
    left, bottom, right, top = grid.bounds
    width = int((right - left + 2 * MARGIN_M) / res)
    height = int((top - bottom + 2 * MARGIN_M) / res)
    return Affine(res, 0, left - MARGIN_M, 0, -res, top + MARGIN_M), (height, width)
