"""Command line entry points: `orbitdiff fetch|mask|mask-report|composites|evaluate|
train-buildings|promote|worker|scheduler|assign-aois`."""

import argparse
import sys
import time
from datetime import UTC, date, datetime
from pathlib import Path

from loguru import logger
from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert

from orbitdiff import (
    accounts,
    buildings,
    composites,
    db,
    detection,
    jobs,
    mask_report,
    masking,
    retraining,
)
from orbitdiff.aoi import load_geometry, make_grid
from orbitdiff.config import REPO_ROOT, PipelineConfig, pipeline_config, settings
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


def _building_model(cfg: PipelineConfig, version: int | None) -> detection.Learned | None:
    """A registered version of the building model if asked for, else the champion, if any."""
    if version is None:
        predictor = buildings.load_champion(settings().data_dir)
    else:
        from orbitdiff import siamese

        retraining.tracked_mlflow()
        predictor = siamese.from_registry(cfg.buildings.registered_model, version)
    return predictor and detection.Learned(predictor, cfg.buildings.threshold)


def _evaluate(args: argparse.Namespace) -> None:
    from orbitdiff import evaluation

    cfg = pipeline_config()
    model = _building_model(cfg, args.model_version)
    names = args.method or ["naive", "cva", "bfast", *(["unet"] if model else []), "production"]
    reports, version, verified = evaluation.evaluate(
        names, cfg, settings().data_dir, args.eval_dir, fetch=args.fetch, buildings=model
    )
    if not reports:
        raise SystemExit(f"No labelled AOIs in {args.eval_dir}: see eval/README.md")
    print(f"Evaluation set {version}" + ("" if verified else " (labels NOT verified)"))
    if model:
        print(f"Building model {model.probability.version}")
    print(evaluation.table(reports))
    if args.no_mlflow:
        return
    evaluation.save_scored(reports, version, verified, settings().data_dir)
    mlflow = retraining.tracked_mlflow()
    mlflow.set_experiment("evaluation")
    for name, report in reports.items():
        with mlflow.start_run(run_name=name):
            mlflow.log_params(
                {"method": name, "eval_set": version, "labels_verified": verified}
                | {"buildings_model": model and model.probability.version}
                | cfg.detection.model_dump(exclude={"categories"})
                | {f"category_{k}": v for k, v in cfg.detection.categories.model_dump().items()}
                | cfg.evaluation.model_dump()
            )
            mlflow.log_metrics(evaluation.metrics(report))


def _train_buildings(args: argparse.Namespace) -> None:
    cfg, data_dir = pipeline_config().buildings, settings().data_dir
    feedback = []
    if args.feedback:
        with db.session() as s:
            feedback = retraining.load_not_real(s, data_dir)
    result = retraining.train_candidate(cfg, data_dir, feedback, log=not args.no_mlflow)
    trained, test = result.trained, result.test
    print(f"Validation ({', '.join(cfg.val_cities)}): F1 {trained.val.f1:.3f}")
    print(
        f"OSCD test, {len(trained.test)} cities: F1 {test.f1:.3f} "
        f"(precision {test.precision:.3f}, recall {test.recall:.3f})"
    )
    for name, s in trained.test.items():
        print(f"  {name:12} F1 {s.f1:.3f}")
    print(f"Trained in {trained.seconds / 60:.1f} min on CPU; kept step {trained.best_step}")
    if args.feedback:
        print(
            f"Feedback: trained on {result.trained_on}, held out {result.held_out}; "
            f"held-out false alarms {result.false_alarms} "
            f"(champion {result.champion_false_alarms})"
        )
    if result.version is not None:
        print(
            f"Registered {cfg.registered_model} version {result.version} "
            f"(run {result.run_id}). Not promoted: see `orbitdiff promote`."
        )


def _promote(args: argparse.Namespace) -> None:
    from orbitdiff import evaluation

    cfg, data_dir = pipeline_config(), settings().data_dir
    aois, eval_version = evaluation.load_eval_set(args.eval_dir)
    unverified = sum(not a.verified for a in aois)
    if not aois or unverified:
        state = "no labelled areas" if not aois else f"{unverified} unverified areas"
        raise SystemExit(
            f"Refusing: promotion needs a verified evaluation set, and {args.eval_dir} has "
            f"{state}. See eval/README.md."
        )
    candidate = _building_model(cfg, args.version)
    champion = _building_model(cfg, None)
    before, _, _ = evaluation.evaluate(
        ["naive", detection.PRODUCTION], cfg, data_dir, args.eval_dir, buildings=champion
    )
    after, _, _ = evaluation.evaluate(
        [detection.PRODUCTION], cfg, data_dir, args.eval_dir, buildings=candidate
    )
    rows = {
        "baseline": before["naive"],
        "champion": before[detection.PRODUCTION],
        "candidate": after[detection.PRODUCTION],
    }
    names = {
        "baseline": "naive",
        "champion": champion.probability.version if champion else "none",
        "candidate": candidate.probability.version,
    }
    print(f"Evaluation set {eval_version}")
    print(evaluation.table({f"{k}: {names[k]}": v for k, v in rows.items()}))
    reasons = evaluation.gate(
        rows["candidate"].overall,
        rows["champion"].overall,
        rows["baseline"].overall,
        cfg.buildings.recall_tolerance,
    )
    mlflow = retraining.tracked_mlflow()
    mlflow.set_experiment("promotion")
    with mlflow.start_run(run_name=f"promote {names['candidate']}"):
        mlflow.log_params(
            {f"{k}_model": v for k, v in names.items()}
            | {"eval_set": eval_version, "promoted": not reasons}
            | {"recall_tolerance": cfg.buildings.recall_tolerance}
        )
        for key, report in rows.items():
            mlflow.log_metrics({f"{key}_{m}": v for m, v in evaluation.metrics(report).items()})
    if reasons:
        raise SystemExit("Not promoted:\n- " + "\n- ".join(reasons))

    from mlflow import MlflowClient

    from orbitdiff import siamese

    client, name = MlflowClient(), cfg.buildings.registered_model
    siamese.save_weights(candidate.probability, buildings.folder(data_dir) / f"v{args.version}.pt")
    run_id = client.get_model_version(name, str(args.version)).run_id
    buildings.write_champion(data_dir, name, args.version, run_id)
    client.set_registered_model_alias(name, buildings.CHAMPION, str(args.version))
    print(f"Promoted {names['candidate']}. Restart the worker to run it.")


def _worker(args: argparse.Namespace) -> None:
    from redis import Redis
    from rq import Queue, SimpleWorker

    redis = Redis.from_url(settings().redis_url)
    # SimpleWorker runs jobs in this process: the cloud model stays loaded between jobs,
    # and macOS can't fork safely once GDAL and PyTorch threads are running.
    queues = [Queue(name, connection=redis) for name in (jobs.QUEUE, jobs.RETRAIN_QUEUE)]
    SimpleWorker(queues, connection=redis).work()


def _scheduler(args: argparse.Namespace) -> None:
    hours = pipeline_config().schedule.poll_every_hours
    while True:
        jobs.poll_watched()
        try:
            jobs.poll_retraining()
        except Exception:  # a problem here must not stop areas being watched
            logger.exception("retraining poll failed")
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
    ev.add_argument("--method", nargs="+", help="default: every method, unet with a model")
    ev.add_argument(
        "--model-version", type=int, help="building model version (default: the champion)"
    )
    ev.add_argument("--eval-dir", type=Path, default=REPO_ROOT / "eval")
    ev.add_argument("--fetch", action="store_true", help="read and composite missing imagery")
    ev.add_argument("--no-mlflow", action="store_true", help="print only, log nothing")
    ev.set_defaults(run=_evaluate)

    tb = sub.add_parser(
        "train-buildings", help="train the building model on OSCD and register it (ml group)"
    )
    tb.add_argument("--no-mlflow", action="store_true", help="print only, register nothing")
    tb.add_argument(
        "--feedback", action="store_true", help='add "Not a real change" labels as hard negatives'
    )
    tb.set_defaults(run=_train_buildings)

    pr = sub.add_parser(
        "promote", help="make a building model the champion, if it passes the gate (ml group)"
    )
    pr.add_argument("--version", type=int, required=True, help="registered model version")
    pr.add_argument("--eval-dir", type=Path, default=REPO_ROOT / "eval")
    pr.set_defaults(run=_promote)

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
