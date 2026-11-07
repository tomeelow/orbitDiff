"""Scoring detection methods on the evaluation set (`eval/`, eval/README.md).

Each `eval/<name>.geojson` is one AOI: its polygon (role "aoi") with the period of imagery
it covers, its hard cases and who verified it, and the events in it (role "event"), each
with a category, a footprint and the month it became visible. Quiet AOIs have no events, so
every alert in them is false.

The numbers are the ones users feel: event-level precision and recall, false alerts per
AOI-year, and detection delay (days from the first clear pass after a change to its
alert), overall, per category and per hard case.
"""

import hashlib
import json
import statistics
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import numpy as np
from rasterio.features import geometry_mask
from shapely.geometry import shape
from shapely.geometry.base import BaseGeometry

from orbitdiff import composites, masking
from orbitdiff.alerts import AlertRule, default_rule, should_alert
from orbitdiff.aoi import AOIGrid, make_grid, reproject
from orbitdiff.config import EvaluationConfig, PipelineConfig
from orbitdiff.detection import (
    CATEGORIES,
    YEAR,
    Event,
    Passes,
    detect,
    load_stack,
    methods,
    month_gap,
    observed_share,
)
from orbitdiff.fetch import fetch_aoi

EVAL_DIR = Path(__file__).resolve().parents[2] / "eval"


@dataclass(frozen=True)
class Label:
    category: str
    month: str  # "YYYY-MM": the first month it is visible from orbit
    footprint: BaseGeometry  # EPSG:4326


@dataclass(frozen=True)
class EvalAoi:
    name: str
    geometry: BaseGeometry  # EPSG:4326
    first: str  # the period of imagery used, "YYYY-MM", inclusive
    last: str
    hard_cases: tuple[str, ...]  # cropland, floodplain, snow, ...
    verified: bool  # every label checked against high-resolution imagery
    labels: tuple[Label, ...]

    @property
    def start(self) -> date:
        return date.fromisoformat(f"{self.first}-01")

    @property
    def evaluated_months(self) -> int:
        """Months that have an anniversary inside the period: the ones a change can show in."""
        return max(0, month_gap(self.first, self.last) + 1 - YEAR)


def load_aoi(path: Path) -> EvalAoi:
    features = json.loads(path.read_text())["features"]
    aois = [f for f in features if f["properties"]["role"] == "aoi"]
    if len(aois) != 1:
        raise ValueError(f"{path.name}: needs exactly one feature with role 'aoi'")
    p = aois[0]["properties"]
    labels = []
    for f in features:
        if f["properties"]["role"] != "event":
            continue
        category = f["properties"]["category"]
        if category not in CATEGORIES:
            raise ValueError(f"{path.name}: unknown category {category!r}")
        labels.append(Label(category, f["properties"]["month"], shape(f["geometry"])))
    return EvalAoi(
        name=path.stem,
        geometry=shape(aois[0]["geometry"]),
        first=p["first_month"],
        last=p["last_month"],
        hard_cases=tuple(p.get("hard_cases", [])),
        verified=bool(p.get("verified_by")),
        labels=tuple(labels),
    )


def load_eval_set(folder: Path = EVAL_DIR) -> tuple[list[EvalAoi], str]:
    """The AOIs and the set's version: a hash of the label files, so any edit shows up next
    to the metrics it changed."""
    paths = sorted(folder.glob("*.geojson"))
    digest = hashlib.sha256()
    for path in paths:
        digest.update(path.name.encode() + path.read_bytes())
    return [load_aoi(p) for p in paths], digest.hexdigest()[:12]


@dataclass(frozen=True)
class Alert:
    """An event as the evaluation sees it: what a user would have been told, and when."""

    footprint: BaseGeometry  # the AOI's UTM zone
    category: str
    month: str
    alerted: np.datetime64


def alerts_from(
    events: Iterable[Event], rule: AlertRule | None = None, confirmed_only: bool = False
) -> list[Alert]:
    """What users would have been told, and when. Without a rule (a bare detector) every
    event alerts when detected. With one, an event alerts when detected, or with
    `confirmed_only` when confirmed, if the rule passes it in the status it had then."""
    alerts = []
    for e in events:
        if rule is None:
            alerts.append(Alert(e.footprint, e.category, e.month, e.detected))
            continue
        status = "confirmed" if confirmed_only else e.history[0][0]
        at = e.first("confirmed") if confirmed_only else e.detected
        if at is not None and should_alert(status, e.category, e.area_ha, rule)[0]:
            alerts.append(Alert(e.footprint, e.category, e.month, at))
    return alerts


@dataclass(frozen=True)
class AoiRun:
    """One method's alerts over one evaluation AOI."""

    aoi: EvalAoi
    labels: tuple[BaseGeometry, ...]  # label footprints in the AOI's UTM zone
    first_clear: tuple[np.datetime64 | None, ...]  # per label: first clear pass once visible
    alerts: tuple[Alert, ...]


def first_clear_passes(
    aoi: EvalAoi, grid: AOIGrid, passes: Passes, min_clear: float
) -> tuple[tuple[BaseGeometry, ...], tuple[np.datetime64 | None, ...]]:
    """Each label's footprint in UTM, and the first pass from its month on that sees enough
    of it clearly: where the delay clock starts."""
    footprints, starts = [], []
    for label in aoi.labels:
        geom = reproject(label.footprint, 4326, grid.epsg)
        pixels = geometry_mask(
            [geom], out_shape=(grid.height, grid.width), transform=grid.transform, invert=True
        )
        start = None
        for k in range(month_gap(label.month, aoi.last) + 1):
            month = str(np.datetime64(label.month, "M") + np.timedelta64(k, "M"))
            start = next(
                (
                    o.acquired
                    for o in passes.in_month(month)
                    if pixels.any() and observed_share(pixels, o.values) >= min_clear
                ),
                None,
            )
            if start is not None:
                break
        footprints.append(geom)
        starts.append(start)
    return tuple(footprints), tuple(starts)


def _matches(alert: Alert, label: Label, footprint: BaseGeometry, cfg: EvaluationConfig) -> bool:
    lag = month_gap(label.month, alert.month)
    if not -cfg.months_before <= lag <= cfg.months_after:
        return False
    smaller = min(alert.footprint.area, footprint.area)
    return smaller > 0 and alert.footprint.intersection(footprint).area >= cfg.min_overlap * smaller


@dataclass
class Scores:
    alerts: int = 0
    true_alerts: int = 0
    labels: int = 0
    found: int = 0
    aoi_years: float = 0.0
    delays: tuple[float, ...] = ()

    @property
    def precision(self) -> float | None:
        return self.true_alerts / self.alerts if self.alerts else None

    @property
    def recall(self) -> float | None:
        return self.found / self.labels if self.labels else None

    @property
    def false_alerts_per_aoi_year(self) -> float | None:
        return (self.alerts - self.true_alerts) / self.aoi_years if self.aoi_years else None

    @property
    def median_delay_days(self) -> float | None:
        return statistics.median(self.delays) if self.delays else None


@dataclass(frozen=True)
class Report:
    overall: Scores
    by_category: dict[str, Scores]  # recall and delay by labelled category, precision by alerted
    by_hard_case: dict[str, Scores]  # over the AOIs tagged with each hard case


def score(runs: Iterable[AoiRun], cfg: EvaluationConfig) -> Report:
    overall = Scores()
    by_category = {c: Scores() for c in CATEGORIES}
    by_hard_case: dict[str, Scores] = {}
    for run in runs:
        groups = [overall] + [by_hard_case.setdefault(h, Scores()) for h in run.aoi.hard_cases]
        matched = [
            [
                _matches(a, label, fp, cfg)
                for label, fp in zip(run.aoi.labels, run.labels, strict=True)
            ]
            for a in run.alerts
        ]
        for alert, hits in zip(run.alerts, matched, strict=True):
            for s in (*groups, by_category[alert.category]):
                s.alerts += 1
                s.true_alerts += any(hits)
        for j, (label, start) in enumerate(zip(run.aoi.labels, run.first_clear, strict=True)):
            times = [a.alerted for a, hits in zip(run.alerts, matched, strict=True) if hits[j]]
            delay = (
                [float((min(times) - start) / np.timedelta64(1, "D"))]
                if times and start is not None
                else []
            )
            for s in (*groups, by_category[label.category]):
                s.labels += 1
                s.found += bool(times)
                s.delays += tuple(delay)
        for s in groups:
            s.aoi_years += run.aoi.evaluated_months / YEAR
    return Report(overall, by_category, by_hard_case)


def _fmt(value: float | None, digits: int = 2) -> str:
    return "—" if value is None else f"{value:.{digits}f}"


def table(reports: dict[str, Report]) -> str:
    """The benchmark table, then where each method fails, as Markdown."""
    lines = [
        "| Method | Event precision | Event recall | False alerts / AOI-yr | Median delay (days) |",
        "|---|---|---|---|---|",
    ]
    for name, r in reports.items():
        s = r.overall
        lines.append(
            f"| {name} | {_fmt(s.precision)} | {_fmt(s.recall)} "
            f"| {_fmt(s.false_alerts_per_aoi_year)} | {_fmt(s.median_delay_days, 0)} |"
        )
    for title, pick in (("category", "by_category"), ("hard case", "by_hard_case")):
        rows = [
            f"| {name} | {k} | {_fmt(s.precision)} | {_fmt(s.recall)} "
            f"| {_fmt(s.false_alerts_per_aoi_year)} |"
            for name, r in reports.items()
            for k, s in sorted(getattr(r, pick).items())
            if s.alerts or s.labels or s.aoi_years
        ]
        if rows:
            lines += [
                "",
                f"| Method | {title} | Precision | Recall | False alerts / AOI-yr |",
                "|---|---|---|---|---|",
                *rows,
            ]
    return "\n".join(lines)


def metrics(report: Report) -> dict[str, float]:
    """Flat metrics for MLflow: overall, then per category and hard case."""
    out: dict[str, float] = {}
    for prefix, scores in (
        ("", {"": report.overall}),
        ("category_", report.by_category),
        ("hard_case_", report.by_hard_case),
    ):
        for key, s in scores.items():
            stem = f"{prefix}{key}_" if key else ""
            for name in ("precision", "recall", "false_alerts_per_aoi_year", "median_delay_days"):
                value = getattr(s, name)
                if value is not None:
                    out[f"{stem}{name}"] = value
    return out


def prepare(aoi: EvalAoi, grid: AOIGrid, cfg: PipelineConfig, data_dir: Path) -> None:
    """Read, mask and composite an evaluation AOI's period. Incremental: only what's missing."""
    end = (np.datetime64(aoi.last, "M") + np.timedelta64(1, "M")).astype("datetime64[D]")
    fetch_aoi(grid, aoi.start, (end - np.timedelta64(1, "D")).item(), cfg, data_dir)
    model = masking.omnicloudmask(cfg.masking.cloud_model_version, data_dir / "models")
    masking.update_masks(grid.aoi_id, cfg.masking, data_dir, model)
    composites.update_composites(grid.aoi_id, cfg, data_dir)


def evaluate(
    names: Iterable[str],
    cfg: PipelineConfig,
    data_dir: Path,
    folder: Path = EVAL_DIR,
    fetch: bool = False,
) -> tuple[dict[str, Report], str, bool]:
    """Score each named method over the evaluation set. Returns the reports, the set's
    version and whether every AOI in it is verified."""
    aois, version = load_eval_set(folder)
    chosen = {name: methods(cfg.detection)[name] for name in names}
    runs: dict[str, list[AoiRun]] = {}
    for aoi in aois:
        grid = make_grid(f"eval-{aoi.name}", aoi.geometry, cfg.aoi.max_km2)
        if fetch:
            prepare(aoi, grid, cfg, data_dir)
        stack = load_stack(data_dir, grid.aoi_id, aoi.first, aoi.last)
        passes = Passes(data_dir, grid.aoi_id, cfg.masking)
        labels, first_clear = first_clear_passes(
            aoi, grid, passes, cfg.detection.footprint_clear_frac
        )
        for name, method in chosen.items():
            events = detect(stack, passes, grid, method, cfg.detection)
            rule = default_rule(cfg.alerts) if method.rules else None
            policies = {name: False} | ({f"{name}, confirmed only": True} if method.rules else {})
            for row, confirmed_only in policies.items():
                alerts = tuple(alerts_from(events, rule, confirmed_only))
                runs.setdefault(row, []).append(AoiRun(aoi, labels, first_clear, alerts))
    verified = bool(aois) and all(a.verified for a in aois)
    return {n: score(r, cfg.evaluation) for n, r in runs.items()}, version, verified
