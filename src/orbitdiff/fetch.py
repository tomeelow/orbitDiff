"""Bring an AOI's cube up to date: search, keep one item per pass, read what's missing.

Running it again only reads passes the cube doesn't have yet, so the same function does
the archive backfill and later incremental updates. Passes that fail to read are left
out and retried on the next run.
"""

import time
from collections import Counter
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import date
from functools import partial
from pathlib import Path

from loguru import logger

from orbitdiff import catalog, cube, reading
from orbitdiff.aoi import AOIGrid
from orbitdiff.config import PipelineConfig

WRITE_EVERY = 64  # passes per Zarr append


@dataclass(frozen=True)
class FetchSummary:
    passes: int  # passes over the AOI in the period
    per_year: dict[int, int]  # passes found per year
    cached: int  # already in the cube
    full: int  # SCL + 10 m bands read
    scl_only: int  # SCL read, AOI too cloudy for the bands
    tile_cloud: int  # skipped on tile cloud cover, nothing read
    failed: int  # every candidate failed; retried next run
    bytes_read: int
    full_bytes: int  # what the same assets weigh as whole files
    seconds: float

    def report(self) -> str:
        share = f"{self.bytes_read / self.full_bytes:.1%}" if self.full_bytes else "n/a"
        return (
            f"{self.passes} passes ({self.cached} already cached): {self.full} read in full, "
            f"{self.scl_only} too cloudy over the AOI, {self.tile_cloud} skipped on tile cloud, "
            f"{self.failed} failed | read {self.bytes_read / 1e6:.1f} MB of "
            f"{self.full_bytes / 1e6:.0f} MB in the same files ({share}) in {self.seconds:.0f}s\n"
            f"passes per year: {', '.join(f'{y}: {n}' for y, n in self.per_year.items())}"
        )


def fetch_aoi(
    grid: AOIGrid,
    start: date,
    end: date,
    cfg: PipelineConfig,
    data_dir: Path,
    on_progress: Callable[[int, int], None] | None = None,
) -> FetchSummary:
    t0 = time.perf_counter()
    catalogs = [cfg.catalog.primary, cfg.catalog.fallback]
    by_name = {c.name: c for c in catalogs}
    items = catalog.search(catalogs, grid.geometry, start, end, cache_dir=data_dir / "stac")
    passes = catalog.group_passes(items, grid, primary=cfg.catalog.primary.name)

    path = cube.cube_path(data_dir, grid.aoi_id)
    existing = cube.load(path)
    if existing is not None and not cube.matches_grid(existing, grid):
        raise ValueError(f"{path} was built on a different grid; an AOI's grid never changes")
    done = cube.pass_keys(existing) if existing is not None else set()
    todo = [candidates for key, candidates in passes.items() if key not in done]
    logger.info("{}: {} passes found, {} to read", grid.aoi_id, len(passes), len(todo))

    def sign(scene: catalog.SceneItem, band: str) -> str:
        return catalog.signed_href(scene.hrefs[band], by_name[scene.catalog])

    read = partial(
        reading.read_pass,
        grid=grid,
        scl_drop=cfg.masking.scl_drop,
        fetch_min_clear_frac=cfg.reading.fetch_min_clear_frac,
        max_tile_cloud_pct=cfg.reading.max_tile_cloud_pct,
        sign=sign,
    )
    counts: Counter[str] = Counter()
    bytes_read = full_bytes = 0
    batch: list[reading.PassRead] = []
    grid.mask  # noqa: B018 - rasterize once here, not concurrently in every worker
    with ThreadPoolExecutor(cfg.reading.workers) as pool:
        futures = {pool.submit(read, candidates): candidates for candidates in todo}
        for n, future in enumerate(as_completed(futures), 1):
            try:
                result = future.result()
            except Exception as exc:  # one pass must not sink the backfill
                logger.warning("pass {} failed: {!r}", futures[future][0].item_id, exc)
                result = None
            if result is None:
                counts["failed"] += 1
            else:
                counts[result.status] += 1
                bytes_read += result.bytes_read
                full_bytes += result.full_bytes
                batch.append(result)
            if len(batch) >= WRITE_EVERY:
                cube.append(path, cube.to_dataset(batch, grid))
                batch = []
            if on_progress:
                on_progress(n, len(todo))
    if batch:
        cube.append(path, cube.to_dataset(batch, grid))

    summary = FetchSummary(
        passes=len(passes),
        per_year=dict(sorted(Counter(day.year for _, day in passes).items())),
        cached=len(passes) - len(todo),
        full=counts["full"],
        scl_only=counts["scl"],
        tile_cloud=counts["tile_cloud"],
        failed=counts["failed"],
        bytes_read=bytes_read,
        full_bytes=full_bytes,
        seconds=time.perf_counter() - t0,
    )
    logger.info("{}: {}", grid.aoi_id, summary.report())
    return summary
