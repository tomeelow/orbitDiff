"""The per-AOI cube: every pass ever read for an AOI, on the AOI's fixed grid, in Zarr.

Fetching is slow and recomputing is cheap, so the cube keeps raw digital numbers plus
each pass's provenance and DN offset. Masking, compositing and detection all start from
here; changing any of them never refetches imagery.

Every pass gets a row, including the ones too cloudy to read in full, so the archive's
real size and cloudiness stay visible. `status` says what was read.
"""

from collections.abc import Sequence
from pathlib import Path

import numpy as np
import xarray as xr

from orbitdiff.aoi import RESOLUTION_M, AOIGrid
from orbitdiff.catalog import PassKey
from orbitdiff.config import BANDS
from orbitdiff.radiometry import to_reflectance
from orbitdiff.reading import PassRead

TIME_CHUNK = 32


def cube_path(data_dir: Path, aoi_id: str) -> Path:
    return data_dir / "cubes" / f"{aoi_id}.zarr"


def _grid_coords(grid: AOIGrid) -> dict[str, np.ndarray]:
    left, _, _, top = grid.bounds
    half = RESOLUTION_M / 2
    return {
        "x": left + half + RESOLUTION_M * np.arange(grid.width),
        "y": top - half - RESOLUTION_M * np.arange(grid.height),
    }


def to_dataset(reads: Sequence[PassRead], grid: AOIGrid) -> xr.Dataset:
    shape = (grid.height, grid.width)
    zeros = np.zeros(shape, np.uint16)

    def stack(pick, dtype) -> np.ndarray:
        return np.stack([pick(r) for r in reads]).astype(dtype, copy=False)

    data = {
        band: (
            ("time", "y", "x"),
            stack(lambda r, b=band: r.bands[b] if r.bands else zeros, np.uint16),
        )
        for band in BANDS
    }
    data["scl"] = (
        ("time", "y", "x"),
        stack(lambda r: r.scl if r.scl is not None else zeros, np.uint8),
    )
    s = [r.scene for r in reads]

    def text(values) -> np.ndarray:
        # Variable-length strings: fixed-width ones lock the width at the first write,
        # and a later, longer item id would fail to append.
        return np.array(values, dtype=object)

    coords = {
        "time": np.array([i.acquired.replace(tzinfo=None) for i in s], dtype="datetime64[ns]"),
        "item_id": ("time", text([i.item_id for i in s])),
        "catalog": ("time", text([i.catalog for i in s])),
        "platform": ("time", text([i.platform for i in s])),
        "tile": ("time", text([i.tile for i in s])),
        "baseline": ("time", text([i.baseline for i in s])),
        "offset_dn": ("time", np.array([i.offset_dn for i in s], np.int16)),
        "tile_cloud_pct": ("time", np.array([i.tile_cloud_pct for i in s], np.float32)),
        "status": ("time", text([r.status for r in reads])),
        "clear_frac": ("time", np.array([r.clear_frac for r in reads], np.float32)),
        "bytes_read": ("time", np.array([r.bytes_read for r in reads], np.int64)),
        "full_bytes": ("time", np.array([r.full_bytes for r in reads], np.int64)),
        **_grid_coords(grid),
    }
    attrs = {
        "aoi_id": grid.aoi_id,
        "aoi_wkt": grid.geometry.wkt,
        "epsg": grid.epsg,
        "transform": list(grid.transform)[:6],
    }
    return xr.Dataset(data, coords=coords, attrs=attrs)


def append(path: Path, ds: xr.Dataset) -> None:
    if path.exists():
        ds.to_zarr(path, append_dim="time", consolidated=False)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    chunks = (TIME_CHUNK, ds.sizes["y"], ds.sizes["x"])
    encoding = {v: {"chunks": chunks} for v in [*BANDS, "scl"]}
    # Fixed units: otherwise xarray picks them from the first batch, and a later batch
    # with an earlier or finer-grained time can't be encoded.
    encoding["time"] = {"units": "microseconds since 1970-01-01", "dtype": "int64"}
    ds.to_zarr(path, mode="w-", encoding=encoding, consolidated=False)


def load(path: Path) -> xr.Dataset | None:
    if not path.exists():
        return None
    return xr.open_zarr(path, consolidated=False).sortby("time")


def matches_grid(ds: xr.Dataset, grid: AOIGrid) -> bool:
    return (
        ds.attrs["epsg"] == grid.epsg
        and np.allclose(ds.attrs["transform"], list(grid.transform)[:6])
        and (ds.sizes["y"], ds.sizes["x"]) == (grid.height, grid.width)
    )


def pass_keys(ds: xr.Dataset) -> set[PassKey]:
    days = ds["time"].values.astype("datetime64[D]").tolist()
    return set(zip(ds["platform"].values.tolist(), days, strict=True))


def reflectance(ds: xr.Dataset) -> xr.Dataset:
    """The 10 m bands as float32 surface reflectance, NaN where there's no data."""
    offset = ds["offset_dn"].values[:, None, None]
    return xr.Dataset(
        {band: (ds[band].dims, to_reflectance(ds[band].values, offset)) for band in BANDS},
        coords={k: ds.coords[k] for k in ("time", "y", "x")},
    )
