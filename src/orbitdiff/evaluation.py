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
import re
import statistics
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path

import numpy as np
from pydantic import BaseModel
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
    Learned,
    Passes,
    detect,
    load_stack,
    methods,
    month_gap,
    observed_share,
)
from orbitdiff.fetch import fetch_aoi

EVAL_DIR = Path(__file__).resolve().parents[2] / "eval"
LABEL_CATEGORIES = tuple(c for c in CATEGORIES if c != "other")  # what a labelled change is
MONTH = re.compile(r"\d{4}-(0[1-9]|1[0-2])")


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


def parse_aoi(name: str, collection: dict) -> EvalAoi:
    """One evaluation file's contents, checked (eval/README.md). The CLI and the app's
    labelling both go through here, so a file one accepts the other scores."""
    features = collection["features"]
    aois = [f for f in features if f["properties"]["role"] == "aoi"]
    if len(aois) != 1:
        raise ValueError(f"{name}: needs exactly one feature with role 'aoi'")
    p = aois[0]["properties"]
    first, last = p["first_month"], p["last_month"]
    for month in (first, last):
        if not MONTH.fullmatch(month):
            raise ValueError(f"{name}: {month!r} is not a month like 2021-05")
    if month_gap(first, last) < 0:
        raise ValueError(f"{name}: the period ends ({last}) before it starts ({first})")
    labels = []
    for i, f in enumerate((f for f in features if f["properties"]["role"] == "event"), 1):
        category, month = f["properties"]["category"], f["properties"]["month"]
        if category not in LABEL_CATEGORIES:
            raise ValueError(f"{name}: change {i} has unknown category {category!r}")
        if not MONTH.fullmatch(month) or month_gap(month, last) < 0:
            raise ValueError(f"{name}: change {i}'s month {month!r} is not in {first} to {last}")
        # Every label counts towards recall, so one no method can find would count as missed.
        if month_gap(first, month) < YEAR:
            raise ValueError(
                f"{name}: change {i} ({month}) is in the period's first year, which has no "
                f"anniversary to compare with; start the period a year earlier or leave it out"
            )
        labels.append(Label(category, month, shape(f["geometry"])))
    return EvalAoi(
        name=name,
        geometry=shape(aois[0]["geometry"]),
        first=first,
        last=last,
        hard_cases=tuple(p.get("hard_cases", [])),
        verified=bool(p.get("verified_by")),
        labels=tuple(labels),
    )


def load_aoi(path: Path) -> EvalAoi:
    return parse_aoi(path.stem, json.loads(path.read_text()))


def dump_aoi(collection: dict) -> str:
    """An evaluation file's text: one feature per line, so editing a label is a one-line diff."""
    features = ",\n".join(json.dumps(f, ensure_ascii=False) for f in collection["features"])
    return '{"type": "FeatureCollection", "features": [\n' + features + "\n]}\n"


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


def f1(s: Scores) -> float | None:
    """Event F1; with labels but no alerts, 0."""
    if s.recall is None:
        return None
    p, r = s.precision or 0.0, s.recall
    return 2 * p * r / (p + r) if p + r else 0.0


def gate(
    candidate: Scores, champion: Scores, baseline: Scores, recall_tolerance: float
) -> list[str]:
    """Why a candidate model may not replace the champion; empty when it may (Phase 8).
    Scored with production's alerts with each model in place, the candidate must not
    raise false alerts per AOI-year, not lose more than `recall_tolerance` of event recall,
    and must beat the naive baseline's event F1."""
    values = (
        candidate.false_alerts_per_aoi_year,
        champion.false_alerts_per_aoi_year,
        candidate.recall,
        champion.recall,
        f1(candidate),
        f1(baseline),
    )
    if any(v is None for v in values):
        return ["the evaluation set can't score it: it needs AOI-years and labelled changes"]
    fa_new, fa_old, recall_new, recall_old, f1_new, f1_base = values
    reasons = []
    if fa_new > fa_old:
        reasons.append(f"false alerts per AOI-year rise from {fa_old:.2f} to {fa_new:.2f}")
    if recall_new < recall_old - recall_tolerance:
        reasons.append(
            f"event recall falls from {recall_old:.2f} to {recall_new:.2f}, "
            f"more than the {recall_tolerance:.2f} allowed"
        )
    if not f1_new > f1_base:
        reasons.append(f"event F1 {f1_new:.2f} doesn't beat the naive baseline's {f1_base:.2f}")
    return reasons


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


class Benchmark(BaseModel):
    """One row of the benchmark table."""

    method: str
    precision: float | None
    recall: float | None
    false_alerts_per_aoi_year: float | None
    median_delay_days: float | None


class Scored(BaseModel):
    """The latest full `orbitdiff evaluate`, kept where the API can show it: MLflow, which
    has every run, isn't in the API's image."""

    eval_set: str  # the set's version: a hash of its label files
    verified: bool  # False: these numbers are not a benchmark
    scored_utc: datetime
    rows: list[Benchmark]


def _scored_path(data_dir: Path) -> Path:
    return data_dir / "reports" / "evaluation.json"


def save_scored(reports: dict[str, Report], version: str, verified: bool, data_dir: Path) -> None:
    rows = [
        Benchmark(
            method=name,
            precision=r.overall.precision,
            recall=r.overall.recall,
            false_alerts_per_aoi_year=r.overall.false_alerts_per_aoi_year,
            median_delay_days=r.overall.median_delay_days,
        )
        for name, r in reports.items()
    ]
    scored = Scored(eval_set=version, verified=verified, scored_utc=datetime.now(UTC), rows=rows)
    path = _scored_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_suffix(".part")  # the API may be reading the last one
    partial.write_text(scored.model_dump_json(indent=2))
    partial.replace(path)


def load_scored(data_dir: Path) -> Scored | None:
    path = _scored_path(data_dir)
    return Scored.model_validate_json(path.read_text()) if path.exists() else None


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
    buildings: Learned | None = None,
) -> tuple[dict[str, Report], str, bool]:
    """Score each named method over the evaluation set, with `buildings` as the building
    model if given. Returns the reports, the set's version and whether every AOI in it is
    verified."""
    aois, version = load_eval_set(folder)
    available = methods(cfg.detection, buildings)
    unknown = set(names) - set(available)
    if unknown:
        raise ValueError(f"unknown methods {sorted(unknown)}; there are {sorted(available)}")
    chosen = {name: available[name] for name in names}
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
