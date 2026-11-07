"""Queue jobs (RQ): an AOI's backfill, from catalog search to change events and alerts.

The backfill is incremental by construction: every step only does what's missing, so
rerunning it after a failure, or later for new passes, goes through the same code. That
rerun is also the new-pass loop: the scheduler queues it for every watched AOI.
Progress lives on the AOI's row, so it outlives the job.
"""

import math
import time
from collections.abc import Callable
from datetime import UTC, date, datetime
from pathlib import Path

import numpy as np
from geoalchemy2.shape import from_shape, to_shape
from loguru import logger
from redis import Redis
from rq import Queue
from shapely.geometry.base import BaseGeometry
from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from orbitdiff import composites, cube, db, detection, masking, notify, render
from orbitdiff.aoi import AOIGrid, reproject
from orbitdiff.config import PipelineConfig, pipeline_config, settings
from orbitdiff.fetch import fetch_aoi

QUEUE = "backfill"
PROGRESS_EVERY = 10  # passes between progress writes
MASK_BATCH = 64  # passes loaded at once when computing clear fractions
UPSERT_ROWS = 1000  # rows per statement; Postgres allows 65,535 parameters


def claim_backfill(s: Session, aoi_id: int) -> bool:
    """Mark an idle AOI (ready, or failed) queued, so it's queued once however many callers
    ask at the same moment. False when it's already queued or running."""
    claimed = s.execute(
        update(db.Aoi)
        .where(db.Aoi.id == aoi_id, db.Aoi.stage.in_(("ready", "failed")))
        .values(stage="queued", error=None)
        .returning(db.Aoi.id)
    ).scalar_one_or_none()
    return claimed is not None


def poll_watched(enqueue: Callable[[int], None] | None = None) -> list[int]:
    """Queue an update for every AOI with alerts on that isn't already updating (ADR-008).
    The update searches only the current year: finished years' searches are cached."""
    enqueue = enqueue or enqueue_backfill
    with db.session() as s:
        watched = s.scalars(
            select(db.AlertRule.aoi_id)
            .join(db.Aoi)
            .where(db.AlertRule.enabled, db.Aoi.user_id.is_not(None))
            .order_by(db.AlertRule.aoi_id)
        ).all()
        queued = [aoi_id for aoi_id in watched if claim_backfill(s, aoi_id)]
    for aoi_id in queued:
        enqueue(aoi_id)
    logger.info("poll: {} watched AOIs, {} queued", len(watched), len(queued))
    return queued


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
        grid, start = aoi.grid(), aoi.archive_start
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
        _set(aoi_id, stage="detecting")
        sync_events(aoi_id, cfg, data_dir)
        _set(aoi_id, stage="ready")
    except Exception as exc:
        _set(aoi_id, stage="failed", error=f"{type(exc).__name__}: {exc}")
        raise
    # After "ready": the archive is complete either way, and a failed email is retried on
    # the next run, not reported as a failed backfill.
    notify.notify(aoi_id)


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


def _utc(t: np.datetime64) -> datetime:
    return t.astype("datetime64[us]").item().replace(tzinfo=UTC)


def _month(month: str) -> date:
    return datetime.strptime(month, "%Y-%m").date()


def _match(
    found: list[detection.Event], geometries: list[BaseGeometry], stored: list[db.Event]
) -> dict[int, db.Event]:
    """Each found event's stored row: from the same month, overlapping it most, best pairs
    first, so an event finds its own earlier self rather than a neighbour or a stale row."""
    by_month: dict[date, list[tuple[db.Event, BaseGeometry]]] = {}
    for r in stored:
        by_month.setdefault(r.month, []).append((r, to_shape(r.geometry)))
    pairs = []
    for i, (e, geometry) in enumerate(zip(found, geometries, strict=True)):
        for r, shape in by_month.get(_month(e.month), []):
            overlap = geometry.intersection(shape).area  # degrees², only compared here
            if overlap > 0:  # touching at an edge is not the same change
                pairs.append((overlap / geometry.union(shape).area, i, r))
    rows: dict[int, db.Event] = {}
    taken: set[int] = set()
    for _, i, r in sorted(pairs, key=lambda p: p[0], reverse=True):
        if i not in rows and r.id not in taken:
            rows[i] = r
            taken.add(r.id)
    return rows


def sync_events(aoi_id: int, cfg: PipelineConfig, data_dir: Path) -> None:
    """Run the production method over the AOI's whole archive and reconcile its events with
    the stored ones. Detection is a replay, so the backfill and a single new pass go through
    the same code. A recomputed event keeps its row (and so its alerts) when it overlaps the
    stored one from the same month; a stored event no longer found is dismissed, never
    deleted."""
    t0 = time.perf_counter()
    key = str(aoi_id)
    with db.session() as s:
        grid = s.get(db.Aoi, aoi_id).grid()
    method = detection.methods(cfg.detection)[detection.PRODUCTION]
    found = detection.detect(
        detection.load_stack(data_dir, key),
        detection.Passes(data_dir, key, cfg.masking),
        grid,
        method,
        cfg.detection,
    )
    version = detection.version(method, cfg.detection)
    now = datetime.now(UTC)
    geometries = [reproject(e.footprint, grid.epsg, 4326) for e in found]
    with db.session() as s:
        stored = list(
            s.scalars(select(db.Event).where(db.Event.aoi_id == aoi_id).order_by(db.Event.id))
        )
        rows = _match(found, geometries, stored)
        unmatched = {e.id for e in stored} - {r.id for r in rows.values()}
        for i, (e, geometry) in enumerate(zip(found, geometries, strict=True)):
            values = {
                "aoi_id": aoi_id,
                "month": _month(e.month),
                "reference_month": _month(e.reference_month),
                "category": e.category,
                "status": e.status,
                "area_ha": e.area_ha,
                "geometry": from_shape(geometry, srid=4326),
                "ndvi_before": None if np.isnan(e.ndvi_before) else e.ndvi_before,
                "ndvi_after": None if np.isnan(e.ndvi_after) else e.ndvi_after,
                "detected_utc": _utc(e.detected),
                "status_utc": _utc(e.history[-1][2]),
                "history": [
                    {"status": st, "cause": cause, "at": _utc(at).isoformat()}
                    for st, cause, at in e.history
                ],
                "items": e.items,
                "method_version": version,
                "thresholds": cfg.detection.model_dump(),
            }
            row = rows.get(i)
            if row is None:
                s.add(db.Event(**values))
            else:
                for name, value in values.items():
                    setattr(row, name, value)
        for row in (r for r in stored if r.id in unmatched and r.status != "dismissed"):
            row.status, row.status_utc = "dismissed", now
            row.history = [
                *row.history,
                {"status": "dismissed", "cause": "no longer detected", "at": now.isoformat()},
            ]
    logger.info(
        "AOI {}: {} events ({}), {} no longer detected, in {:.1f}s",
        aoi_id,
        len(found),
        version,
        len(unmatched),
        time.perf_counter() - t0,
    )
