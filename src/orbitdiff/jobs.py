"""Queue jobs (RQ): an AOI's backfill, from catalog search to monthly composites.

The backfill is incremental by construction: every step only does what's missing, so
rerunning it after a failure, or later for new passes, goes through the same code.
Progress lives on the AOI's row, so it outlives the job.
"""

import math
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
from geoalchemy2.shape import to_shape
from loguru import logger
from redis import Redis
from rq import Queue
from sqlalchemy import update
from sqlalchemy.dialects.postgresql import insert

from orbitdiff import composites, cube, db, masking, render
from orbitdiff.aoi import AOIGrid, make_grid
from orbitdiff.config import PipelineConfig, pipeline_config, settings
from orbitdiff.fetch import fetch_aoi

QUEUE = "backfill"
PROGRESS_EVERY = 10  # passes between progress writes
MASK_BATCH = 64  # passes loaded at once when computing clear fractions
UPSERT_ROWS = 1000  # rows per statement; Postgres allows 65,535 parameters


def grid_for(aoi: db.Aoi) -> AOIGrid:
    # The size cap was enforced when the AOI was created; a later, lower cap must not
    # strand existing AOIs.
    return make_grid(str(aoi.id), to_shape(aoi.geometry), max_km2=math.inf)


def enqueue_backfill(aoi_id: int) -> None:
    queue = Queue(QUEUE, connection=Redis.from_url(settings().redis_url))
    hours = pipeline_config().jobs.backfill_timeout_hours
    queue.enqueue(backfill, aoi_id, job_id=f"backfill-{aoi_id}", job_timeout=int(hours * 3600))


def _set(aoi_id: int, **values: object) -> None:
    with db.session() as s:
        s.execute(update(db.Aoi).where(db.Aoi.id == aoi_id).values(**values))


def backfill(aoi_id: int, model: masking.CloudModel | None = None) -> None:
    cfg, data_dir = pipeline_config(), settings().data_dir
    with db.session() as s:
        aoi = s.get(db.Aoi, aoi_id)
        if aoi is None:
            raise LookupError(f"no AOI {aoi_id}")
        grid, start = grid_for(aoi), aoi.archive_start
    logger.info("backfill {}: {:.2f} km², EPSG:{}", aoi_id, grid.area_km2, grid.epsg)

    def progress(done: int, total: int) -> None:
        if done % PROGRESS_EVERY == 0 or done == total:
            _set(aoi_id, progress_done=done, progress_total=total)

    try:
        _set(aoi_id, stage="reading", progress_done=0, progress_total=0, error=None)
        fetch_aoi(grid, start, datetime.now(UTC).date(), cfg, data_dir, on_progress=progress)
        _set(aoi_id, stage="masking")
        if model is None:
            model = masking.omnicloudmask(cfg.masking.cloud_model_version, data_dir / "models")
        masking.update_masks(grid.aoi_id, cfg.masking, data_dir, model)
        _set(aoi_id, stage="compositing")
        composites.update_composites(grid.aoi_id, cfg, data_dir)
        sync_records(aoi_id, cfg, data_dir)
        _set(aoi_id, stage="ready")
    except Exception as exc:
        _set(aoi_id, stage="failed", error=f"{type(exc).__name__}: {exc}")
        raise


def _mask_stats(ds, masks, grid: AOIGrid, cfg: PipelineConfig) -> dict[str, tuple[float, bool]]:
    """SCL + model clear fraction and offset check of every pass with bands, in batches
    to bound memory."""
    full = np.flatnonzero(ds["status"].values == "full")
    out: dict[str, tuple[float, bool]] = {}
    for batch in np.array_split(full, max(1, math.ceil(len(full) / MASK_BATCH))):
        sel = ds.isel(time=batch).load()
        clear = masking.clear_mask(sel, masking.model_classes_for(sel, masks), cfg.masking)
        fracs = masking.clear_fraction(clear, grid.mask).tolist()
        suspect = masking.offset_suspect(sel, cfg.masking).tolist()
        items = sel["item_id"].values.tolist()
        out.update(zip(items, zip(fracs, suspect, strict=True), strict=True))
    return out


def _ndvi_mean(path: Path, grid: AOIGrid) -> float | None:
    _, _, red, nir = composites.read_composite(path)
    values = render.ndvi(red, nir)[grid.mask]
    values = values[~np.isnan(values)]
    return round(float(values.mean()), 4) if values.size else None


def sync_records(aoi_id: int, cfg: PipelineConfig, data_dir: Path) -> None:
    """Mirror the AOI's cube and composites into its scenes and months rows."""
    key = str(aoi_id)
    ds = cube.load(cube.cube_path(data_dir, key))
    masks = cube.load(masking.mask_path(data_dir, key))
    grid = cube.grid_of(ds)
    stats = _mask_stats(ds, masks, grid, cfg)
    for item in (item for item, (_, suspect) in stats.items() if suspect):
        logger.warning("AOI {}: {} left out, its offset flag contradicts its pixels", aoi_id, item)

    scenes = []
    for t in range(ds.sizes["time"]):
        item, status = str(ds["item_id"].values[t]), str(ds["status"].values[t])
        scl_frac = float(ds["clear_frac"].values[t])
        clear_frac, suspect = stats.get(item, (None if math.isnan(scl_frac) else scl_frac, False))
        scenes.append(
            {
                "aoi_id": aoi_id,
                "item_id": item,
                "catalog": str(ds["catalog"].values[t]),
                "acquired_utc": ds["time"]
                .values[t]
                .astype("datetime64[us]")
                .item()
                .replace(tzinfo=UTC),
                "platform": str(ds["platform"].values[t]),
                "mgrs_tile": str(ds["tile"].values[t]),
                "processing_baseline": str(ds["baseline"].values[t]),
                "tile_cloud_pct": float(ds["tile_cloud_pct"].values[t]),
                "read_status": status,
                "aoi_clear_frac": clear_frac,
                "offset_suspect": suspect,
                "bytes_read": int(ds["bytes_read"].values[t]),
                "full_bytes": int(ds["full_bytes"].values[t]),
            }
        )
    out = composites.composite_dir(data_dir, key)
    months = [
        {
            "aoi_id": aoi_id,
            "month": datetime.strptime(m.month, "%Y-%m").date(),
            "passes": m.passes,
            "with_bands": m.with_bands,
            "clear_scenes": m.clear_scenes,
            "observed_frac": m.observed_frac,
            "filled_frac": m.filled_frac,
            "ndvi_mean": _ndvi_mean(out / f"{m.month}.tif", grid),
            "version": m.display_inputs,
        }
        for m in composites.read_months(data_dir, key)
    ]
    with db.session() as s:
        for table, rows, keys in (
            (db.Scene, scenes, ("aoi_id", "item_id")),
            (db.Month, months, ("aoi_id", "month")),
        ):
            for start in range(0, len(rows), UPSERT_ROWS):
                stmt = insert(table).values(rows[start : start + UPSERT_ROWS])
                s.execute(
                    stmt.on_conflict_do_update(
                        index_elements=list(keys),
                        set_={c: stmt.excluded[c] for c in rows[0] if c not in keys},
                    )
                )
    logger.info("AOI {}: {} scenes and {} months recorded", aoi_id, len(scenes), len(months))
