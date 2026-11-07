"""Monthly composites: one cloud-free, pixel-aligned image per AOI per month (ADR-004).

Each month is the per-pixel median of that month's clear observations, written as a COG
(blue, green, red, nir as reflectance x 10000, plus n_clear and n_snow). A pixel with no
clear observation stays empty: this is what detection reads.

A second, display-only COG per month fills empty pixels from the nearest clear months on
each side (at most `fill_max_months` away), averaging the two, and flags every filled
pixel. Filled pixels are never evidence of change.

`update_composites` is the only way composites get made. It recomputes a month when the
passes, masks or settings behind it changed, and a display month when any month in its
fill window did, so the archive backfill and a single new pass go through the same code.
"""

import hashlib
import json
import os
import time
import warnings
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np
import rasterio
import xarray as xr
from loguru import logger

from orbitdiff import cube, masking
from orbitdiff.aoi import AOIGrid
from orbitdiff.config import BANDS, PipelineConfig

SCALE = 10_000  # stored value = reflectance x SCALE; 0 = no data
DETECTION_BANDS = (*BANDS, "n_clear", "n_snow")
DISPLAY_BANDS = (*BANDS, "filled")


@dataclass(frozen=True)
class Month:
    month: str  # "2021-07", UTC acquisition dates
    passes: int  # every pass over the AOI that month
    with_bands: int  # passes whose 10 m bands were read
    clear_scenes: int  # passes with at least scene_min_clear_frac of the AOI clear
    observed_frac: float  # share of AOI pixels with at least one clear observation
    filled_frac: float  # share of AOI pixels filled for display
    inputs: str  # fingerprint of everything the composite was computed from
    display_inputs: str  # fingerprint of the months its display fill used


def composite_dir(data_dir: Path, aoi_id: str) -> Path:
    return data_dir / "composites" / aoi_id


def months_between(first: np.datetime64, last: np.datetime64) -> list[str]:
    start, end = first.astype("datetime64[M]"), last.astype("datetime64[M]")
    return [str(m) for m in np.arange(start, end + np.timedelta64(1, "M"))]


def median_composite(
    refl: xr.Dataset, clear: np.ndarray, snow: np.ndarray
) -> dict[str, np.ndarray]:
    """Per-pixel median of clear observations; NaN where a pixel has none."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)  # all-NaN pixels stay NaN
        out = {b: np.nanmedian(np.where(clear, refl[b].values, np.nan), axis=0) for b in BANDS}
    out["n_clear"] = clear.sum(axis=0)
    out["n_snow"] = (clear & snow).sum(axis=0)
    return out


def _nearest_observed(months: Iterable[np.ndarray]) -> np.ndarray | None:
    """Per pixel, the first month (nearest first) that has a value; NaN if none does."""
    out = None
    for m in months:
        if out is None:
            out = np.full_like(m, np.nan)
        take = np.isnan(out[0]) & ~np.isnan(m[0])
        out[:, take] = m[:, take]
    return out


def gap_fill(
    target: np.ndarray, before: list[np.ndarray], after: list[np.ndarray]
) -> tuple[np.ndarray, np.ndarray]:
    """Fill a month's empty pixels (bands, y, x) for display.

    `before` and `after` are the neighbouring months' unfilled composites, nearest first.
    A pixel takes the mean of the nearest clear value on each side, or the one side that
    has one. Returns the filled bands and a mask of the pixels that were filled.
    """
    sides = [s for s in (_nearest_observed(before), _nearest_observed(after)) if s is not None]
    empty = np.isnan(target[0])
    if not sides or not empty.any():
        return target, np.zeros(empty.shape, bool)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)  # neither side has a value
        fill = np.nanmean(np.stack(sides), axis=0)
    out = target.copy()
    out[:, empty] = fill[:, empty]
    return out, empty & ~np.isnan(out[0])


def _to_uint16(refl: np.ndarray) -> np.ndarray:
    return np.where(np.isnan(refl), 0, np.clip(np.round(refl * SCALE), 1, 65535)).astype(np.uint16)


def _write_cog(path: Path, bands: dict[str, np.ndarray], grid: AOIGrid, tags: dict) -> None:
    """Write atomically, so a tile server never sees half a file."""
    tmp = path.with_suffix(".tmp.tif")
    profile = {
        "driver": "COG",
        "width": grid.width,
        "height": grid.height,
        "count": len(bands),
        "dtype": "uint16",
        "crs": grid.crs,
        "transform": grid.transform,
        "nodata": 0,
        "compress": "deflate",
        "predictor": "yes",
        "blocksize": 256,
    }
    with rasterio.open(tmp, "w", **profile) as dst:
        for i, (name, data) in enumerate(bands.items(), 1):
            dst.write(data.astype(np.uint16), i)
            dst.set_band_description(i, name)
        dst.update_tags(**tags)
    os.replace(tmp, path)


def read_composite(path: Path) -> np.ndarray:
    """The four reflectance bands of a detection COG, float32 with NaN for no data."""
    with rasterio.open(path) as src:
        dn = src.read(list(range(1, len(BANDS) + 1)))
    return np.where(dn == 0, np.nan, dn / SCALE).astype(np.float32)


def _json_default(o: object) -> object:
    return sorted(o) if isinstance(o, set | frozenset) else str(o)


def _fingerprint(*parts: object) -> str:
    text = json.dumps(parts, sort_keys=True, default=_json_default)
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def _read_index(path: Path) -> dict[str, Month]:
    if not path.exists():
        return {}
    return {m["month"]: Month(**m) for m in json.loads(path.read_text())["months"]}


def _write_index(path: Path, aoi_id: str, months: dict[str, Month]) -> None:
    tmp = path.with_suffix(".tmp")
    body = {"aoi_id": aoi_id, "months": [asdict(m) for _, m in sorted(months.items())]}
    tmp.write_text(json.dumps(body, indent=1))
    os.replace(tmp, path)


def _aoi_share(pixels: np.ndarray, grid: AOIGrid) -> float:
    return round(float((pixels & grid.mask).sum() / grid.mask.sum()), 4)


@dataclass(frozen=True)
class CompositeSummary:
    months: int
    computed: int  # detection composites (re)computed
    displayed: int  # display composites (re)computed
    seconds: float


def update_composites(aoi_id: str, cfg: PipelineConfig, data_dir: Path) -> CompositeSummary:
    t0 = time.perf_counter()
    ds = cube.load(cube.cube_path(data_dir, aoi_id))
    if ds is None or ds.sizes["time"] == 0:
        raise FileNotFoundError(f"no cube for {aoi_id}; fetch it first")
    masks = cube.load(masking.mask_path(data_dir, aoi_id))
    grid = cube.grid_of(ds)
    out = composite_dir(data_dir, aoi_id)
    out.mkdir(parents=True, exist_ok=True)
    index_path = out / "months.json"
    index = _read_index(index_path)

    pass_month = ds["time"].values.astype("datetime64[M]").astype(str)
    months = months_between(ds["time"].values[0], ds["time"].values[-1])
    settings = (cfg.masking.model_dump(), SCALE)
    computed = 0
    for month in months:
        sel = ds.isel(time=np.flatnonzero(pass_month == month)).load()
        inputs = _fingerprint(
            settings, sel["item_id"].values.tolist(), sel["status"].values.tolist()
        )
        if month in index and index[month].inputs == inputs:
            continue
        classes = masking.model_classes_for(sel, masks)
        clear = masking.clear_mask(sel, classes, cfg.masking)
        snow = masking.snow(sel["scl"].values, cfg.masking.scl_flag)
        comp = median_composite(cube.reflectance(sel), clear, snow)
        scene_clear = masking.clear_fraction(clear, grid.mask)
        bands = {b: _to_uint16(comp[b]) for b in BANDS} | {
            "n_clear": comp["n_clear"],
            "n_snow": comp["n_snow"],
        }
        _write_cog(out / f"{month}.tif", bands, grid, {"month": month, "inputs": inputs})
        index[month] = Month(
            month=month,
            passes=sel.sizes["time"],
            with_bands=int((sel["status"].values == "full").sum()),
            clear_scenes=int((scene_clear >= cfg.masking.scene_min_clear_frac).sum()),
            observed_frac=_aoi_share(comp["n_clear"] > 0, grid),
            filled_frac=0.0,
            inputs=inputs,
            display_inputs="",
        )
        computed += 1

    # Display composites read the stored (quantized) detection COGs, never in-memory
    # values, so backfill and incremental updates produce identical files.
    k = cfg.composites.fill_max_months
    cache: dict[str, np.ndarray] = {}

    def unfilled(month: str) -> np.ndarray:
        if month not in cache:
            cache[month] = read_composite(out / f"{month}.tif")
        return cache[month]

    displayed = 0
    for i, month in enumerate(months):
        before, after = months[max(0, i - k) : i][::-1], months[i + 1 : i + 1 + k]
        display_inputs = _fingerprint([index[m].inputs for m in (*before, month, *after)])
        if index[month].display_inputs == display_inputs:
            continue
        filled, was_filled = gap_fill(
            unfilled(month), [unfilled(m) for m in before], [unfilled(m) for m in after]
        )
        bands = {b: _to_uint16(filled[j]) for j, b in enumerate(BANDS)} | {"filled": was_filled}
        _write_cog(
            out / f"{month}_display.tif", bands, grid, {"month": month, "inputs": display_inputs}
        )
        index[month] = Month(
            **asdict(index[month])
            | {
                "filled_frac": _aoi_share(was_filled, grid),
                "display_inputs": display_inputs,
            }
        )
        displayed += 1

    _write_index(index_path, aoi_id, index)
    summary = CompositeSummary(len(months), computed, displayed, time.perf_counter() - t0)
    logger.info(
        "{}: {} months, {} composites and {} display composites computed in {:.1f}s",
        aoi_id,
        summary.months,
        computed,
        displayed,
        summary.seconds,
    )
    return summary


def read_months(data_dir: Path, aoi_id: str) -> list[Month]:
    """The AOI's months in order, for the timeline."""
    return [
        m for _, m in sorted(_read_index(composite_dir(data_dir, aoi_id) / "months.json").items())
    ]
