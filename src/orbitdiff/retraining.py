"""A retraining candidate from "Not a real change" feedback (Phase 10).

Each one says that these pixels, between these two months, didn't change, and nothing about
the rest of the area, so only the footprint is labelled. They are hard negatives for the
building model, mixed into training on OSCD. They can't teach it what it missed: people only
see what alerted.

Feedback is split by area, never by pixel or event. An area that overlaps the evaluation set
stays out altogether, or the promotion gate would score a model on what it trained on.

A candidate is only ever registered. Replacing the champion takes `orbitdiff promote`, run by
a person, through the gate on the evaluation set.

No PyTorch at import: the scheduler decides whether a candidate is due.
"""

import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from functools import lru_cache
from pathlib import Path

import numpy as np
from loguru import logger
from rasterio.features import geometry_mask
from shapely.geometry import mapping, shape
from shapely.ops import unary_union
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from orbitdiff import buildings, composites, db, evaluation, monitoring
from orbitdiff.aoi import reproject
from orbitdiff.config import BuildingsConfig, PipelineConfig, settings

MODEL_CATEGORY = "buildings_roads"  # what the trained model finds


@dataclass(frozen=True)
class NotReal:
    """One "Not a real change", on the composites the detector compared."""

    event_id: int
    aoi_id: int
    before: np.ndarray  # (band, y, x) reflectance over the whole area, NaN where unseen
    after: np.ndarray
    footprint: np.ndarray  # (y, x) bool, only pixels seen on both dates


@lru_cache(maxsize=256)  # many labels come from the same few months
def _composite(data_dir: Path, aoi_id: int, month: str) -> np.ndarray | None:
    path = composites.composite_dir(data_dir, str(aoi_id)) / f"{month[:7]}.tif"
    return composites.read_composite(path) if path.exists() else None


def load_not_real(s: Session, data_dir: Path) -> list[NotReal]:
    """Every "Not a real change" that can be trained on."""
    if not evaluation.EVAL_DIR.is_dir():  # silently empty, it would let the gate's areas in
        raise RuntimeError(f"no evaluation set at {evaluation.EVAL_DIR}: can't keep it out")
    eval_areas, _ = evaluation.load_eval_set(evaluation.EVAL_DIR)
    scored = unary_union([a.geometry for a in eval_areas])
    items, in_eval_set, unusable = [], 0, 0
    grids: dict[int, object] = {}
    for event in s.scalars(
        select(db.Feedback.event).where(db.Feedback.verdict == "not_real").order_by(db.Feedback.id)
    ):
        aoi_id = event["aoi_id"]
        grid = grids.setdefault(aoi_id, s.get(db.Aoi, aoi_id).grid())
        if grid.geometry.intersects(scored):
            in_eval_set += 1
            continue
        before = _composite(data_dir, aoi_id, event["reference_month"])
        after = _composite(data_dir, aoi_id, event["month"])
        if before is None or after is None:
            unusable += 1
            continue
        footprint = geometry_mask(
            [mapping(reproject(shape(event["geometry"]), 4326, grid.epsg))],
            out_shape=(grid.height, grid.width),
            transform=grid.transform,
            invert=True,
        )
        footprint &= np.isfinite(before).all(axis=0) & np.isfinite(after).all(axis=0)
        if not footprint.any():
            unusable += 1
            continue
        items.append(NotReal(event["event_id"], aoi_id, before, after, footprint))
    _composite.cache_clear()  # the worker lives on: what's still needed is held by `items`
    logger.info(
        "feedback for training: {} usable, {} in the evaluation set, {} without pixels",
        len(items),
        in_eval_set,
        unusable,
    )
    return items


def split(items: Sequence[NotReal], every: int) -> tuple[list[NotReal], list[NotReal]]:
    """(to train on, held out), each area wholly on one side: one area in every `every`
    is held out. A lone area can't be split by area, so it is all trained on."""
    areas = sorted({i.aoi_id for i in items})
    held = set(areas[::every]) if len(areas) > 1 else set()
    return [i for i in items if i.aoi_id not in held], [i for i in items if i.aoi_id in held]


def false_alarm_share(predictor, items: Sequence[NotReal], threshold: float) -> float | None:
    """Of the pixels people said didn't change, the share a model calls changed as served."""
    called, total = 0, 0
    for item in items:
        probability = predictor(item.before, item.after)[item.footprint]
        known = probability[np.isfinite(probability)]
        called += int((known > threshold).sum())
        total += known.size
    return called / total if total else None


def _feedback_version(items: Sequence[NotReal]) -> str:
    return hashlib.sha256(json.dumps(sorted(i.event_id for i in items)).encode()).hexdigest()[:12]


def tracked_mlflow():
    """MLflow, pointed at this deployment's tracking store."""
    import mlflow

    uri = settings().mlflow_tracking_uri
    if uri.startswith("sqlite:///"):  # SQLite won't make the folder
        Path(uri.removeprefix("sqlite:///")).parent.mkdir(parents=True, exist_ok=True)
    mlflow.set_tracking_uri(uri)
    return mlflow


def _use_experiment(mlflow, name: str, data_dir: Path) -> None:
    """Models stay with the rest of the data, wherever the job runs from."""
    if mlflow.get_experiment_by_name(name) is None:
        location = (data_dir / "mlruns").resolve().as_uri()
        mlflow.create_experiment(name, artifact_location=location)
    mlflow.set_experiment(name)


@dataclass
class Candidate:
    trained: object  # siamese.Trained, without importing torch here
    test: object  # siamese.Scores, summed over OSCD's test cities
    trained_on: int  # "Not a real change"s it trained on
    held_out: int  # ...and those it never saw
    false_alarms: float | None  # share of held-out pixels it still calls changed
    champion_false_alarms: float | None  # the same for the champion
    version: int | None = None  # as registered; None if it wasn't logged
    run_id: str | None = None


def train_candidate(
    cfg: BuildingsConfig, data_dir: Path, feedback: Sequence[NotReal] = (), log: bool = True
) -> Candidate:
    """Train a model, score it, and register it unless told not to. Never promotes it."""
    import torch

    from orbitdiff import oscd, siamese

    folder = oscd.download(cfg.oscd, data_dir / "oscd")
    to_train, held_out = split(feedback, cfg.feedback_val_every)
    negatives = [
        siamese.hard_negative(i.before, i.after, i.footprint, cfg.patch_px) for i in to_train
    ]
    trained = siamese.train(cfg, folder, negatives)
    champion = buildings.load_champion(data_dir)
    candidate = Candidate(
        trained,
        sum(trained.test.values(), siamese.Scores(0, 0, 0)),
        len(to_train),
        len(held_out),
        false_alarm_share(trained.predictor, held_out, cfg.threshold),
        champion and false_alarm_share(champion, held_out, cfg.threshold),
    )
    if not log:
        return candidate
    test = candidate.test
    mlflow = tracked_mlflow()
    _use_experiment(mlflow, "buildings", data_dir)
    run_name = "siamese_unet_rgbn" + ("_feedback" if negatives else "")
    with mlflow.start_run(run_name=run_name) as run:
        mlflow.log_params(
            cfg.model_dump(exclude={"oscd", "val_cities"})
            | {
                "bands": "B02,B03,B04,B08",
                "input": "per-image standardised reflectance",
                "dataset": "OSCD" + (" + feedback hard negatives" if negatives else ""),
                "dataset_version": oscd.version(cfg.oscd)
                + (f"+{_feedback_version(feedback)}" if negatives else ""),
                "train_cities": ",".join(
                    c for c in oscd.cities(folder, "train") if c not in cfg.val_cities
                ),
                "val_cities": ",".join(cfg.val_cities),
                "test_cities": ",".join(trained.test),
                "feedback_trained_on": candidate.trained_on,
                "feedback_held_out": candidate.held_out,
                "pos_weight": round(trained.pos_weight, 3),
                "best_step": trained.best_step,
                "parameters": sum(p.numel() for p in trained.predictor.model.parameters()),
                "torch": torch.__version__,
                "device": "cpu",
            }
        )
        for step, loss, f1 in trained.history:
            mlflow.log_metrics({"train_loss": loss, "val_f1": f1}, step=step)
        figures = {
            "val_precision": trained.val.precision,
            "val_recall": trained.val.recall,
            "oscd_test_precision": test.precision,
            "oscd_test_recall": test.recall,
            "oscd_test_f1": test.f1,
            "train_seconds": trained.seconds,
            "feedback_held_out_false_alarms": candidate.false_alarms,
            "feedback_held_out_false_alarms_champion": candidate.champion_false_alarms,
        } | {f"oscd_test_f1_{name}": s.f1 for name, s in trained.test.items()}
        mlflow.log_metrics({k: v for k, v in figures.items() if v is not None})
        info = siamese.log_model(trained.predictor, cfg.registered_model)
    candidate.version = int(info.registered_model_version)
    candidate.run_id = run.info.run_id
    return candidate


def due(s: Session, cfg: PipelineConfig, data_dir: Path, now: datetime) -> int | None:
    """The feedback count to retrain from, if a candidate is due: the model is running, the
    admin view flags its category under it, and enough has been said since the last one."""
    if buildings.champion_info(data_dir) is None:
        return None
    flagged = any(
        row.flagged and row.category == MODEL_CATEGORY and "+" in row.method_version
        for row in monitoring.summary(s, cfg, now).precision
    )
    if not flagged:
        return None
    labels = s.scalar(
        select(func.count()).select_from(db.Feedback).where(db.Feedback.verdict == "not_real")
    )
    last = buildings.candidate_info(data_dir)
    since = labels - (last["labels"] if last else 0)
    return labels if since >= cfg.monitoring.retrain_min_new_labels else None
