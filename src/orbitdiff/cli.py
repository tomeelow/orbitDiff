"""Command line entry points: `orbitdiff fetch ...`."""

import argparse
import sys
from datetime import UTC, date, datetime
from pathlib import Path

from loguru import logger

from orbitdiff.aoi import load_geometry, make_grid
from orbitdiff.config import pipeline_config, settings
from orbitdiff.fetch import fetch_aoi


def _fetch(args: argparse.Namespace) -> None:
    cfg = pipeline_config()
    grid = make_grid(args.id or args.aoi.stem, load_geometry(args.aoi), cfg.aoi.max_km2)
    logger.info(
        "{}: {:.2f} km², EPSG:{}, {}x{} px",
        grid.aoi_id,
        grid.area_km2,
        grid.epsg,
        grid.width,
        grid.height,
    )
    start = args.start or cfg.aoi.archive_start
    end = args.end or datetime.now(UTC).date()
    summary = fetch_aoi(grid, start, end, cfg, settings().data_dir)
    print(summary.report())


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="orbitdiff")
    sub = parser.add_subparsers(dest="command", required=True)

    fetch = sub.add_parser("fetch", help="read an AOI's passes into its cube (only missing ones)")
    fetch.add_argument("--aoi", type=Path, required=True, help="GeoJSON file with one polygon")
    fetch.add_argument("--id", help="AOI id (default: the file name)")
    fetch.add_argument("--start", type=date.fromisoformat, help="default: archive start in config")
    fetch.add_argument("--end", type=date.fromisoformat, help="default: today (UTC)")
    fetch.set_defaults(run=_fetch)

    args = parser.parse_args(argv)
    logger.remove()
    logger.add(sys.stderr, level="INFO")
    args.run(args)
