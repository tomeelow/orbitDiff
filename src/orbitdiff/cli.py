"""Command line entry points: `orbitdiff fetch|mask|mask-report|composites|evaluate|worker|
scheduler|assign-aois`."""

import argparse
import sys
import time
from datetime import UTC, date, datetime
from pathlib import Path

from loguru import logger
from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert

from orbitdiff import accounts, composites, db, jobs, mask_report, masking
from orbitdiff.aoi import load_geometry, make_grid
from orbitdiff.config import REPO_ROOT, pipeline_config, settings
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


def _mask(args: argparse.Namespace) -> None:
    cfg = pipeline_config().masking
    model = masking.omnicloudmask(cfg.cloud_model_version, settings().data_dir / "models")
    print(masking.update_masks(args.id, cfg, settings().data_dir, model))


def _mask_report(args: argparse.Namespace) -> None:
    path, scores = mask_report.write_report(args.id, pipeline_config().masking, settings().data_dir)
    for s in scores:
        print(
            f"{s.method}: lets through {s.let_through}, throws away {s.thrown_away} of {s.labelled}"
        )
    print(f"report: {path}")


def _composites(args: argparse.Namespace) -> None:
    print(composites.update_composites(args.id, pipeline_config(), settings().data_dir))


def _evaluate(args: argparse.Namespace) -> None:
    from orbitdiff import evaluation

    cfg = pipeline_config()
    reports, version, verified = evaluation.evaluate(
        args.method, cfg, settings().data_dir, args.eval_dir, fetch=args.fetch
    )
    if not reports:
        raise SystemExit(f"No labelled AOIs in {args.eval_dir}: see eval/README.md")
    print(f"Evaluation set {version}" + ("" if verified else " (labels NOT verified)"))
    print(evaluation.table(reports))
    if args.no_mlflow:
        return
    import mlflow

    mlflow.set_tracking_uri(settings().mlflow_tracking_uri)
    mlflow.set_experiment("evaluation")
    for name, report in reports.items():
        with mlflow.start_run(run_name=name):
            mlflow.log_params(
                {"method": name, "eval_set": version, "labels_verified": verified}
                | cfg.detection.model_dump(exclude={"categories"})
                | {f"category_{k}": v for k, v in cfg.detection.categories.model_dump().items()}
                | cfg.evaluation.model_dump()
            )
            mlflow.log_metrics(evaluation.metrics(report))


def _worker(args: argparse.Namespace) -> None:
    from redis import Redis
    from rq import Queue, SimpleWorker

    redis = Redis.from_url(settings().redis_url)
    # SimpleWorker runs jobs in this process: the cloud model stays loaded between jobs,
    # and macOS can't fork safely once GDAL and PyTorch threads are running.
    SimpleWorker([Queue(jobs.QUEUE, connection=redis)], connection=redis).work()


def _scheduler(args: argparse.Namespace) -> None:
    hours = pipeline_config().schedule.poll_every_hours
    while True:
        jobs.poll_watched()
        if args.once:
            return
        time.sleep(hours * 3600)


def _assign_aois(args: argparse.Namespace) -> None:
    """Give the AOIs drawn before accounts existed an owner, so someone can see them."""
    email = accounts.normalize_email(args.email)
    if email is None:
        raise SystemExit(f"not an email address: {args.email}")
    with db.session() as s:
        s.execute(insert(db.User).values(email=email).on_conflict_do_nothing())
        user_id = s.scalars(select(db.User.id).where(db.User.email == email)).one()
        assigned = s.scalars(
            update(db.Aoi)
            .where(db.Aoi.user_id.is_(None))
            .values(user_id=user_id)
            .returning(db.Aoi.id)
        ).all()
    print(f"{len(assigned)} AOIs now belong to {email}: {sorted(assigned)}")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="orbitdiff")
    sub = parser.add_subparsers(dest="command", required=True)

    fetch = sub.add_parser("fetch", help="read an AOI's passes into its cube (only missing ones)")
    fetch.add_argument("--aoi", type=Path, required=True, help="GeoJSON file with one polygon")
    fetch.add_argument("--id", help="AOI id (default: the file name)")
    fetch.add_argument("--start", type=date.fromisoformat, help="default: archive start in config")
    fetch.add_argument("--end", type=date.fromisoformat, help="default: today (UTC)")
    fetch.set_defaults(run=_fetch)

    mask = sub.add_parser("mask", help="run the cloud model on new passes (needs the ml group)")
    mask.add_argument("--id", required=True, help="AOI id")
    mask.set_defaults(run=_mask)

    report = sub.add_parser("mask-report", help="contact sheet: SCL alone vs SCL + model")
    report.add_argument("--id", required=True, help="AOI id")
    report.set_defaults(run=_mask_report)

    comp = sub.add_parser("composites", help="bring monthly composites up to date")
    comp.add_argument("--id", required=True, help="AOI id")
    comp.set_defaults(run=_composites)

    ev = sub.add_parser("evaluate", help="score detection methods on the evaluation set")
    ev.add_argument(
        "--method", nargs="+", default=["naive", "cva", "bfast", "production"], help="methods"
    )
    ev.add_argument("--eval-dir", type=Path, default=REPO_ROOT / "eval")
    ev.add_argument("--fetch", action="store_true", help="read and composite missing imagery")
    ev.add_argument("--no-mlflow", action="store_true", help="print only, log nothing")
    ev.set_defaults(run=_evaluate)

    worker = sub.add_parser("worker", help="run queued backfills (needs the ml group)")
    worker.set_defaults(run=_worker)

    scheduler = sub.add_parser("scheduler", help="queue updates for watched AOIs, every few hours")
    scheduler.add_argument("--once", action="store_true", help="poll once and exit")
    scheduler.set_defaults(run=_scheduler)

    assign = sub.add_parser("assign-aois", help="give AOIs with no owner to an account")
    assign.add_argument("--email", required=True, help="the owner's sign-in address")
    assign.set_defaults(run=_assign_aois)

    args = parser.parse_args(argv)
    logger.remove()
    logger.add(sys.stderr, level="INFO")
    args.run(args)
