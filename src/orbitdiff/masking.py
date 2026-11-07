"""Per-pixel clear masks: SCL first, a learned cloud model on top.

SCL misses thin cirrus, haze and some shadows, and those slip into composites as change.
OmniCloudMask, run on the red, green and NIR bands already in the cube, catches much of
it. A pixel is clear only when its bands have data, SCL doesn't drop it, and the model
doesn't call it cloud or shadow. Every cloud and shadow is first grown by `dilate_px`,
because cloud edges are where masks fail. Snow is kept and flagged, never dropped.

The model's raw classes are cached per AOI next to the cube. Changing the dilation or
the classes counted as cloud never reruns the model, and nothing here refetches imagery.
"""

import time
from collections.abc import Callable, Collection
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import xarray as xr
from loguru import logger

from orbitdiff import cube
from orbitdiff.config import BANDS, MaskingConfig

SCL_NODATA = 0
MODEL_CLEAR = 0  # OmniCloudMask classes: 0 clear, 1 thick cloud, 2 thin cloud, 3 shadow
MODEL_MIN_SIDE = 64  # pad smaller AOIs with no-data; the model needs >= 32 px of context
MODEL_BATCH = 64  # passes per append to the mask store
MODEL_PATCH = 1000  # the model's default patch size

# Red, green, NIR reflectance (y, x) with NaN for no data -> model classes (y, x).
CloudModel = Callable[[np.ndarray, np.ndarray, np.ndarray], np.ndarray]


def dilate(mask: np.ndarray, px: int) -> np.ndarray:
    """Grow True regions by px pixels in every direction (a square), over the last two axes."""
    if px == 0:
        return mask
    height, width = mask.shape[-2:]
    padded = np.pad(mask, [(0, 0)] * (mask.ndim - 2) + [(px, px), (px, px)])
    grown = np.zeros_like(mask)
    for dy in range(2 * px + 1):
        for dx in range(2 * px + 1):
            grown |= padded[..., dy : dy + height, dx : dx + width]
    return grown


def scl_clear(scl: np.ndarray, scl_drop: Collection[int], dilate_px: int) -> np.ndarray:
    """True where SCL shows usable ground. No data is excluded but not grown: swath edges
    aren't clouds."""
    bad = np.isin(scl, sorted(set(scl_drop) - {SCL_NODATA}))
    return (scl != SCL_NODATA) & ~dilate(bad, dilate_px)


def model_clear(classes: np.ndarray, cloud_classes: Collection[int], dilate_px: int) -> np.ndarray:
    return ~dilate(np.isin(classes, sorted(cloud_classes)), dilate_px)


def snow(scl: np.ndarray, scl_flag: Collection[int]) -> np.ndarray:
    return np.isin(scl, sorted(scl_flag))


def clear_mask(
    passes: xr.Dataset, model_classes: np.ndarray | None, cfg: MaskingConfig
) -> np.ndarray:
    """Clear pixels (time, y, x) for a selection of cube passes.

    `model_classes` lines up with `passes` along time. None means SCL alone; that's for
    comparing methods, never for compositing.
    """
    has_data = np.all([passes[band].values != 0 for band in BANDS], axis=0)
    clear = has_data & scl_clear(passes["scl"].values, cfg.scl_drop, cfg.dilate_px)
    if model_classes is not None:
        clear &= model_clear(model_classes, cfg.cloud_model_classes, cfg.dilate_px)
    return clear


def clear_fraction(clear: np.ndarray, aoi_mask: np.ndarray) -> np.ndarray:
    """Share of the AOI's pixels that are clear, per pass."""
    return (clear & aoi_mask).sum(axis=(-2, -1)) / aoi_mask.sum()


def omnicloudmask(version: float, model_dir: Path) -> CloudModel:
    """OmniCloudMask at a pinned model version. Imported lazily: it needs the `ml` group."""
    try:
        from omnicloudmask import predict_from_array
    except ImportError as exc:
        raise ImportError("the cloud model needs the ml group: uv sync --group ml") from exc

    def predict(red: np.ndarray, green: np.ndarray, nir: np.ndarray) -> np.ndarray:
        stack = np.stack([red, green, nir]).astype(np.float32)
        height, width = stack.shape[1:]
        pad = [(0, 0), (0, max(0, MODEL_MIN_SIDE - height)), (0, max(0, MODEL_MIN_SIDE - width))]
        padded = np.pad(stack, pad, constant_values=np.nan)
        side = min(padded.shape[1:], default=MODEL_MIN_SIDE)
        classes = predict_from_array(
            padded,
            patch_size=min(side, MODEL_PATCH),  # one patch per small AOI
            patch_overlap=min(side, MODEL_PATCH) // 2,
            no_data_value=np.nan,
            model_version=version,
            destination_model_dir=model_dir,
        )
        return classes[0, :height, :width].astype(np.uint8)

    return predict


def mask_path(data_dir: Path, aoi_id: str) -> Path:
    return data_dir / "masks" / f"{aoi_id}.zarr"


@dataclass(frozen=True)
class MaskSummary:
    passes: int  # passes with bands in the cube
    cached: int  # already had model output
    seconds: float


def update_masks(aoi_id: str, cfg: MaskingConfig, data_dir: Path, model: CloudModel) -> MaskSummary:
    """Run the cloud model on every cube pass with bands that doesn't have output yet.

    Same shape as `fetch_aoi`: running it again only processes new passes, so it serves
    both the archive backfill and incremental updates.
    """
    t0 = time.perf_counter()
    ds = cube.load(cube.cube_path(data_dir, aoi_id))
    if ds is None:
        raise FileNotFoundError(f"no cube for {aoi_id}; fetch it first")
    path = mask_path(data_dir, aoi_id)
    existing = cube.load(path)
    if existing is not None and (
        existing.attrs["model"],
        existing.attrs["model_version"],
    ) != (cfg.cloud_model, cfg.cloud_model_version):
        raise ValueError(
            f"{path} holds {existing.attrs['model']} {existing.attrs['model_version']}; "
            f"config says {cfg.cloud_model} {cfg.cloud_model_version}. Rerunning every "
            "pass is a deliberate step: move the old store aside first."
        )
    done = set(existing["item_id"].values.tolist()) if existing is not None else set()
    full = ds.isel(time=np.flatnonzero(ds["status"].values == "full"))
    todo = full.isel(time=[i for i, item in enumerate(full["item_id"].values) if item not in done])
    logger.info("{}: {} passes with bands, {} to mask", aoi_id, full.sizes["time"], len(todo.time))

    for start in range(0, todo.sizes["time"], MODEL_BATCH):
        batch = todo.isel(time=slice(start, start + MODEL_BATCH))
        refl = cube.reflectance(batch)
        classes = np.stack(
            [
                model(refl["red"].values[t], refl["green"].values[t], refl["nir"].values[t])
                for t in range(batch.sizes["time"])
            ]
        )
        cube.append(
            path,
            xr.Dataset(
                {"model_class": (("time", "y", "x"), classes)},
                coords={
                    "time": batch["time"].values,
                    "item_id": ("time", batch["item_id"].values.astype(object)),
                    "y": ds["y"].values,
                    "x": ds["x"].values,
                },
                attrs={
                    "aoi_id": aoi_id,
                    "model": cfg.cloud_model,
                    "model_version": cfg.cloud_model_version,
                },
            ),
        )
    summary = MaskSummary(
        passes=full.sizes["time"],
        cached=full.sizes["time"] - todo.sizes["time"],
        seconds=time.perf_counter() - t0,
    )
    logger.info("{}: masked {} new passes in {:.0f}s", aoi_id, len(todo.time), summary.seconds)
    return summary


def model_classes_for(passes: xr.Dataset, masks: xr.Dataset | None) -> np.ndarray:
    """The model's classes lined up with `passes`. Raises if a pass with bands has none:
    a composite must never quietly fall back to SCL alone."""
    if masks is None:
        by_item, stored = {}, None
    else:
        by_item = {item: i for i, item in enumerate(masks["item_id"].values.tolist())}
        stored = masks["model_class"].values
    out = np.zeros((passes.sizes["time"], passes.sizes["y"], passes.sizes["x"]), np.uint8)
    for t, (item, status) in enumerate(
        zip(passes["item_id"].values, passes["status"].values, strict=True)
    ):
        if status != "full":
            continue  # no bands, nothing for the model to see; has_data masks it anyway
        if item not in by_item:
            raise LookupError(f"no cloud model output for {item}; run update_masks")
        out[t] = stored[by_item[item]]
    return out
