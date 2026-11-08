"""Change detection: from an AOI's monthly composites to change events.

Detectors compare like with like. The anniversary detectors compare a month with the same
month a year earlier; the seasonal model compares it with what a season-trend fit of the
pixel's own history expects for that time of year. Only pixels clearly observed on both
sides can change, so gaps, clouds and swath edges never do, and gap-filled display
composites never reach this module. Changed pixels lose 1-2 pixel misregistration slivers
to a morphological opening, group into regions, and regions under the minimum mapping unit
are dropped. Footprints are polygons in the AOI's UTM zone.

A change shows against its reference for a year after it happens, so a pixel belongs to at
most one event per year.

With `rules` (the production method), an event follows the false-alert rule in CLAUDE.md:
- a single clear pass must show it: a composite is a median, and a change no real pass
  shows is not a change;
- it starts `pending`, and the next clear pass over its footprint confirms or dismisses it;
- it is `seasonal` if snow covers its footprint in either month compared, if the footprint
  looked like this in the same month of an earlier year (`seasonal_history_years`), or if
  it reverts within `seasonal_revert_months`. A snow
  event holds no pixels, so a change the snow hid shows once it melts.
Every status records the pass or month that caused it.

`detect` is the one path for the backfill, new passes and the evaluation set.
"""

import hashlib
import json
import warnings
import weakref
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

import numpy as np
from rasterio import features
from scipy import ndimage
from shapely.geometry import shape
from shapely.geometry.base import BaseGeometry
from shapely.ops import unary_union

from orbitdiff import composites, cube, masking
from orbitdiff.aoi import RESOLUTION_M, AOIGrid
from orbitdiff.config import BANDS, CategoryConfig, DetectionConfig, MaskingConfig
from orbitdiff.render import ndvi

BLUE, GREEN, RED, NIR = range(len(BANDS))  # the band axis of every array here
PIXEL_HA = RESOLUTION_M**2 / 1e4
CATEGORIES = ("buildings_roads", "vegetation_loss", "solar_industry", "water", "other")
STATUSES = ("pending", "confirmed", "dismissed", "seasonal")
PRODUCTION = "production"  # the method the product runs
YEAR = 12  # months
CACHED_MONTHS = 2 * YEAR + 2  # pass months kept in memory: a reference year and a margin
FIT_CHUNK = 20_000  # pixels per batch of the seasonal fit, to bound memory
CLAIM_RING_PX = 2  # an event also holds this ring of pixels around it for its year
KEPT_VERDICTS = 256  # a learned detector's verdicts kept, ~90 kB each on a 300 px AOI
# Bump when a change to the code here changes what becomes an event or its status; it goes
# into every event's method version along with the thresholds.
RULES_REVISION = 2  # 2: snow is seasonal; seasonal means "looked like this before"


def shift_month(month: str, months: int) -> str:
    return str(np.datetime64(month, "M") + np.timedelta64(months, "M"))


def month_gap(first: str, second: str) -> int:
    """Months from `first` to `second`."""
    return int((np.datetime64(second, "M") - np.datetime64(first, "M")).astype(int))


@dataclass(frozen=True)
class Stack:
    """An AOI's detection composites: (month, band, y, x) reflectance over consecutive
    months, NaN where a month has no clear observation."""

    months: tuple[str, ...]
    values: np.ndarray
    snow: np.ndarray | None = None  # (month, y, x): snow in a clear observation that month

    def __post_init__(self) -> None:
        # One view per month, the same object every time: a learned detector keeps its
        # verdicts by the arrays it was given.
        object.__setattr__(self, "_frames", tuple(self.values))

    def index(self, month: str) -> int:
        return month_gap(self.months[0], month) if self.months else -1

    def at(self, month: str) -> np.ndarray | None:
        i = self.index(month)
        return self._frames[i] if 0 <= i < len(self.months) else None

    def snowy(self, month: str) -> np.ndarray | None:
        i = self.index(month)
        return self.snow[i] if self.snow is not None and 0 <= i < len(self.months) else None


def load_stack(data_dir: Path, aoi_id: str, first: str = "", last: str = "9999-12") -> Stack:
    """The detection composites between `first` and `last`, inclusive."""
    months = tuple(
        m.month for m in composites.read_months(data_dir, aoi_id) if first <= m.month <= last
    )
    if not months:
        return Stack(months, np.empty((0, len(BANDS), 0, 0)))
    paths = [composites.composite_dir(data_dir, aoi_id) / f"{m}.tif" for m in months]
    return Stack(
        months,
        np.stack([composites.read_composite(p) for p in paths]),
        np.stack([composites.read_snow(p) for p in paths]),
    )


@dataclass(frozen=True)
class Observation:
    """One pass: its reflectance (band, y, x) where clear, NaN elsewhere."""

    item_id: str
    acquired: np.datetime64
    values: np.ndarray


class Passes:
    """An AOI's passes with bands, a month at a time, read from the cube when first asked
    and masked exactly as the composites were."""

    def __init__(self, data_dir: Path, aoi_id: str, cfg: MaskingConfig) -> None:
        ds = cube.load(cube.cube_path(data_dir, aoi_id))
        if ds is None:
            raise FileNotFoundError(f"no cube for {aoi_id}; fetch it first")
        self._ds = ds.isel(time=np.flatnonzero(ds["status"].values == "full"))
        self._masks = cube.load(masking.mask_path(data_dir, aoi_id))
        self._cfg = cfg
        self._month = self._ds["time"].values.astype("datetime64[M]").astype(str)
        self._cache: OrderedDict[str, list[Observation]] = OrderedDict()

    def in_month(self, month: str) -> list[Observation]:
        if month in self._cache:
            self._cache.move_to_end(month)
            return self._cache[month]
        sel = self._ds.isel(time=np.flatnonzero(self._month == month)).load()
        clear = masking.clear_mask(sel, masking.model_classes_for(sel, self._masks), self._cfg)
        refl = cube.reflectance(sel)
        values = np.stack([refl[b].values for b in BANDS], axis=1)  # (time, band, y, x)
        values[np.broadcast_to(~clear[:, None], values.shape)] = np.nan
        obs = [
            Observation(str(item), t, v)
            for item, t, v in zip(sel["item_id"].values, sel["time"].values, values, strict=True)
        ]
        self._cache[month] = obs
        if len(self._cache) > CACHED_MONTHS:
            self._cache.popitem(last=False)
        return obs


# Detectors


def _ndvi(values: np.ndarray) -> np.ndarray:
    return ndvi(values[RED], values[NIR])


class Detector(Protocol):
    def reference(self, stack: Stack, month: str, history_end: str) -> np.ndarray | None:
        """What no change looks like in `month`, from history before `history_end`
        (exclusive); None when there's nothing to compare with."""

    def comparable(self, reference: np.ndarray) -> np.ndarray:
        """Pixels (y, x) the reference can speak for."""

    def changed(self, reference: np.ndarray, observation: np.ndarray) -> np.ndarray:
        """Changed pixels (y, x) of an observation (band, y, x). NaN compares False, so
        missing is never changed."""


class _Anniversary:
    """References are the same month a year earlier."""

    def reference(self, stack: Stack, month: str, history_end: str) -> np.ndarray | None:
        return stack.at(shift_month(month, -YEAR))

    def comparable(self, reference: np.ndarray) -> np.ndarray:
        return ~np.isnan(reference[0])


@dataclass(frozen=True)
class NdviDrop(_Anniversary):
    """The naive baseline: NDVI fell by more than `threshold`. Sees vegetation loss only."""

    threshold: float

    def changed(self, reference: np.ndarray, observation: np.ndarray) -> np.ndarray:
        return _ndvi(reference) - _ndvi(observation) > self.threshold


@dataclass(frozen=True)
class Cva(_Anniversary):
    """Change vector analysis: the length of the four-band reflectance difference."""

    magnitude: float

    def changed(self, reference: np.ndarray, observation: np.ndarray) -> np.ndarray:
        return np.sqrt(((observation - reference) ** 2).sum(axis=0)) > self.magnitude


@dataclass(frozen=True)
class SeasonalModel:
    """BFAST Monitor's season-trend model on NDVI. Per pixel, a linear trend and annual
    harmonics are fitted to the `history_years` before the change; an observation whose NDVI
    falls more than `sigmas` residual standard deviations below the fit is a break. A harvest
    inside the usual crop cycle stays inside the fit; a field turned building site doesn't.

    BFAST's MOSUM over several months is replaced by the confirmation rule: the next clear
    pass must agree."""

    history_years: int
    harmonics: int
    min_months: int
    sigmas: float
    min_sigma: float

    def _design(self, t: np.ndarray) -> np.ndarray:
        """Regressors for month numbers `t`: constant, trend (per year), harmonics."""
        angle = 2 * np.pi * t[:, None] * np.arange(1, self.harmonics + 1) / YEAR
        return np.column_stack([np.ones_like(t), t / YEAR, np.sin(angle), np.cos(angle)])

    def reference(self, stack: Stack, month: str, history_end: str) -> np.ndarray | None:
        """(expected NDVI, residual sigma) per pixel, NaN where the history is too thin."""
        end = stack.index(history_end)
        start = max(0, end - self.history_years * YEAR)
        if end - start < self.min_months:
            return None
        y = _ndvi(stack.values[start:end].transpose(1, 0, 2, 3))  # (time, y, x)
        t = np.arange(start, end, dtype=np.float64)
        x, x_at = self._design(t), self._design(np.array([float(stack.index(month))]))[0]
        k = x.shape[1]
        flat = y.reshape(len(t), -1)
        expected = np.full(flat.shape[1], np.nan)
        sigma = np.full(flat.shape[1], np.nan)
        for lo in range(0, flat.shape[1], FIT_CHUNK):
            obs = flat[:, lo : lo + FIT_CHUNK].T  # (pixel, time)
            w = ~np.isnan(obs)
            n = w.sum(axis=1)
            xw = x[None] * w[..., None]  # (pixel, time, k): unobserved months weigh nothing
            beta = np.linalg.solve(
                xw.transpose(0, 2, 1) @ x + 1e-9 * np.eye(k),
                (xw.transpose(0, 2, 1) @ np.nan_to_num(obs)[..., None]),
            )[..., 0]
            resid = np.where(w, obs - beta @ x.T, 0.0)
            dof = np.maximum(n - k, 1)
            fit = n >= self.min_months
            expected[lo : lo + FIT_CHUNK] = np.where(fit, beta @ x_at, np.nan)
            sigma[lo : lo + FIT_CHUNK] = np.where(
                fit, np.maximum(np.sqrt((resid**2).sum(axis=1) / dof), self.min_sigma), np.nan
            )
        return np.stack([expected, sigma]).reshape(2, *y.shape[1:])

    def comparable(self, reference: np.ndarray) -> np.ndarray:
        return ~np.isnan(reference[0])

    def changed(self, reference: np.ndarray, observation: np.ndarray) -> np.ndarray:
        expected, sigma = reference
        return _ndvi(observation) - expected < -self.sigmas * sigma


class Probability(Protocol):
    """A trained model: P(change) per pixel (y, x) between two images (band, y, x), NaN
    where either is missing. `siamese.Predictor` is one."""

    version: str

    def __call__(self, before: np.ndarray, after: np.ndarray) -> np.ndarray: ...


@dataclass(frozen=True)
class Learned(_Anniversary):
    """A trained model's verdict against the same month a year earlier (Phase 8).

    `detect` asks about the same reference and pass once per event it checks there, and a
    model costs ~50 ms a time, so verdicts are kept for the arrays they were given (by
    identity, while both are alive)."""

    probability: Probability
    threshold: float
    _verdicts: OrderedDict = field(default_factory=OrderedDict, compare=False, repr=False)

    def changed(self, reference: np.ndarray, observation: np.ndarray) -> np.ndarray:
        key = (id(reference), id(observation))
        kept = self._verdicts.get(key)
        if kept and kept[0]() is reference and kept[1]() is observation:
            self._verdicts.move_to_end(key)
            return kept[2]
        verdict = self.probability(reference, observation) > self.threshold
        self._verdicts[key] = (weakref.ref(reference), weakref.ref(observation), verdict)
        if len(self._verdicts) > KEPT_VERDICTS:
            self._verdicts.popitem(last=False)
        return verdict


@dataclass(frozen=True)
class AnyOf:
    """Changed where any detector says so."""

    detectors: tuple[Detector, ...]

    def reference(self, stack: Stack, month: str, history_end: str) -> list | None:
        refs = [d.reference(stack, month, history_end) for d in self.detectors]
        return None if all(r is None for r in refs) else refs

    def comparable(self, reference: list) -> np.ndarray:
        return np.any(
            [
                d.comparable(r)
                for d, r in zip(self.detectors, reference, strict=True)
                if r is not None
            ],
            axis=0,
        )

    def changed(self, reference: list, observation: np.ndarray) -> np.ndarray:
        return np.any(
            [
                d.changed(r, observation)
                for d, r in zip(self.detectors, reference, strict=True)
                if r is not None
            ],
            axis=0,
        )


@dataclass(frozen=True)
class Method:
    """A detector, and whether the false-alert rules apply to what it finds."""

    name: str
    detector: Detector
    rules: bool


def methods(cfg: DetectionConfig, buildings: Learned | None = None) -> dict[str, Method]:
    """The detection methods. With a building model (the promoted champion, or a candidate
    being scored), it is a method of its own, "unet", and one of production's detectors."""
    naive = NdviDrop(cfg.ndvi_drop)
    cva = Cva(cfg.cva_magnitude)
    seasonal = SeasonalModel(
        cfg.seasonal_history_years,
        cfg.seasonal_harmonics,
        cfg.seasonal_min_months,
        cfg.seasonal_sigmas,
        cfg.seasonal_min_sigma,
    )
    found = {
        "naive": Method("naive", naive, rules=False),
        "cva": Method("cva", cva, rules=False),
        "bfast": Method("bfast", seasonal, rules=False),
    }
    detectors: tuple[Detector, ...] = (naive, cva, seasonal)
    if buildings is not None:
        found["unet"] = Method("unet", buildings, rules=False)
        detectors += (buildings,)
    return found | {PRODUCTION: Method(PRODUCTION, AnyOf(detectors), rules=True)}


def _model(method: Method) -> Learned | None:
    detectors = method.detector.detectors if isinstance(method.detector, AnyOf) else ()
    return next((d for d in (method.detector, *detectors) if isinstance(d, Learned)), None)


def thresholds(method: Method, cfg: DetectionConfig) -> dict:
    """The thresholds in force for a method, recorded on every event."""
    model = _model(method)
    return cfg.model_dump() | ({"buildings_threshold": model.threshold} if model else {})


def version(method: Method, cfg: DetectionConfig) -> str:
    """Changes whenever the method, its rules, any threshold or its model does; on every
    event. A model's version is spelt out: `production-1a2b3c4d+orbitdiff_buildings/3`."""
    model = _model(method)
    parts = [method.name, RULES_REVISION, thresholds(method, cfg)]
    if model:  # without one, the same version as before models existed
        parts.append(model.probability.version)
    digest = hashlib.sha256(json.dumps(parts, sort_keys=True).encode()).hexdigest()[:8]
    return f"{method.name}-{digest}" + (f"+{model.probability.version}" if model else "")


# From pixels to events


def change_regions(changed: np.ndarray, grid: AOIGrid, min_area_ha: float) -> list[np.ndarray]:
    """Connected groups of changed pixels inside the AOI, slivers removed, at least
    `min_area_ha` big: one (y, x) mask each."""
    # A 3 x 3 square: strips 1-2 px wide can't hold it, rectangles keep their corners.
    clean = ndimage.binary_opening(changed & grid.mask, structure=np.ones((3, 3), bool))
    labels, n = ndimage.label(clean)
    sizes = np.bincount(labels.ravel(), minlength=n + 1)
    return [labels == i for i in range(1, n + 1) if sizes[i] * PIXEL_HA >= min_area_ha]


def footprint(pixels: np.ndarray, grid: AOIGrid) -> BaseGeometry:
    """A pixel mask as a polygon in the AOI's UTM zone."""
    shapes = features.shapes(pixels.astype(np.uint8), mask=pixels, transform=grid.transform)
    return unary_union([shape(geom) for geom, _ in shapes])


def categorize(before: np.ndarray, after: np.ndarray, cfg: CategoryConfig) -> str:
    """The category of a change from the median reflectance (band,) over its footprint
    before and after. Starting heuristics, replaced only if they fail on the evaluation set."""
    ndvi_before, ndvi_after = _ndvi(before), _ndvi(after)
    ndwi_before, ndwi_after = ((v[GREEN] - v[NIR]) / (v[GREEN] + v[NIR]) for v in (before, after))
    visible_before, visible_after = before[:NIR].mean(), after[:NIR].mean()
    if ndwi_after > cfg.water_ndwi >= ndwi_before:
        return "water"
    if (
        ndvi_after < cfg.built_ndvi
        and visible_after < cfg.dark_visible
        and after[NIR] < cfg.dark_nir
    ):
        return "solar_industry"
    if ndvi_before >= cfg.vegetation_ndvi and ndvi_after < cfg.bare_ndvi:
        return "vegetation_loss"
    if ndvi_after < cfg.built_ndvi and visible_after > visible_before:
        return "buildings_roads"
    return "other"


@dataclass
class Event:
    method: str
    month: str  # the composite month it first showed in
    reference_month: str  # the same month a year earlier, for the before/after view
    pixels: np.ndarray  # (y, x) footprint on the AOI's grid
    footprint: BaseGeometry  # the same, as a polygon in the AOI's UTM zone
    category: str
    ndvi_before: float  # medians over the footprint, from the two months
    ndvi_after: float
    detected: np.datetime64  # the pass it was first seen on
    items: list[str] = field(default_factory=list)  # passes clear over it in both months
    status: str = "pending"
    history: list[tuple[str, str, np.datetime64]] = field(default_factory=list)
    # With rules: the share of the footprint that changed on each pass that decided it.
    shares: list[float] = field(default_factory=list)

    @property
    def area_ha(self) -> float:
        return round(float(self.pixels.sum() * PIXEL_HA), 2)

    @property
    def confidence(self) -> float | None:
        """How much of the footprint changed on the passes that decided it, 0 to 1. A
        measured share, not a probability that the change is real."""
        return round(float(np.mean(self.shares)), 3) if self.shares else None

    def set(self, status: str, cause: str, at: np.datetime64) -> None:
        """Move to `status`, recording what caused it (a pass or a month) and when."""
        self.status = status
        self.history.append((status, cause, at))

    def first(self, status: str) -> np.datetime64 | None:
        return next((at for s, _, at in self.history if s == status), None)


def observed_share(pixels: np.ndarray, *layers: np.ndarray) -> float:
    """Share of the footprint observed in every one of `layers` (band, y, x)."""
    seen = pixels.copy()
    for layer in layers:
        seen &= ~np.isnan(layer[0])
    return float(seen.sum() / pixels.sum())


def agreement(
    detector: Detector,
    reference: np.ndarray | list,
    observation: np.ndarray,
    pixels: np.ndarray,
    cfg: DetectionConfig,
) -> float | None:
    """The share of the footprint an observation shows as changed: None when too little of
    the footprint is seen on both sides to tell."""
    seen = pixels & detector.comparable(reference) & ~np.isnan(observation[0])
    if seen.sum() < cfg.footprint_clear_frac * pixels.sum():
        return None
    return float(detector.changed(reference, observation)[seen].mean())


class _References:
    """Detector references by (month, history end), computed once each."""

    def __init__(self, stack: Stack, detector: Detector) -> None:
        self._stack, self._detector, self._cache = stack, detector, {}

    def get(self, month: str, history_end: str):
        key = (month, history_end)
        if key not in self._cache:
            self._cache[key] = self._detector.reference(self._stack, month, history_end)
        return self._cache[key]


def _first_clear(month: str, pixels: np.ndarray, passes: Passes, cfg: DetectionConfig):
    """Without rules: the first pass in `month` clear over enough of the footprint, else the
    month's last pass (the composite's latest input), else the month's start."""
    obs = passes.in_month(month)
    for o in obs:
        if observed_share(pixels, o.values) >= cfg.footprint_clear_frac:
            return o.item_id, o.acquired
    return (obs[-1].item_id, obs[-1].acquired) if obs else ("", np.datetime64(month, "D"))


def _first_showing(month, pixels, passes, refs, detector, cfg):
    """With rules: the first pass in `month` that shows the change itself, how much of the
    footprint it shows changed, or None."""
    reference = refs.get(month, month)
    for o in passes.in_month(month):
        share = agreement(detector, reference, o.values, pixels, cfg)
        if share is not None and share >= cfg.agree_frac:
            return o.item_id, o.acquired, share
    return None


def _median(values: np.ndarray | None, pixels: np.ndarray) -> np.ndarray:
    """Per-band median over the footprint; NaN where none of it was observed."""
    if values is None:
        return np.full(len(BANDS), np.nan)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)  # all-NaN footprint: stays NaN
        return np.nanmedian(values[:, pixels], axis=1)


def _snow_month(
    stack: Stack, months: tuple[str, ...], pixels: np.ndarray, cfg: DetectionConfig
) -> str | None:
    """The first of `months` with snow on at least `snow_frac` of the footprint it observed."""
    for month in months:
        snow, values = stack.snowy(month), stack.at(month)
        if snow is None or values is None:
            continue
        seen = pixels & ~np.isnan(values[0])
        if seen.any() and snow[seen].mean() >= cfg.snow_frac:
            return month
    return None


def _looked_like_this(
    stack: Stack, month: str, pixels: np.ndarray, cfg: DetectionConfig
) -> str | None:
    """The latest same month, within `seasonal_history_years`, in which the footprint looked
    as it does now: a flood every April, a field in a crop rotation. A clearing or a new
    building has not looked like this before."""
    now = stack.at(month)
    for years in range(1, cfg.seasonal_history_years + 1):
        earlier = shift_month(month, -years * YEAR)
        then = stack.at(earlier)
        if now is None or then is None:
            continue
        seen = pixels & ~np.isnan(then[0]) & ~np.isnan(now[0])
        if seen.sum() < cfg.footprint_clear_frac * pixels.sum():
            continue
        # Alike: neither anniversary detector sees a change between the two, either way.
        differs = (np.abs(_ndvi(now) - _ndvi(then)) > cfg.ndvi_drop) | Cva(
            cfg.cva_magnitude
        ).changed(then, now)
        if differs[seen].mean() < cfg.agree_frac:
            return earlier
    return None


def _follow(
    event: Event, seen_on: str, stack: Stack, passes: Passes, refs: _References, detector, cfg
) -> None:
    """Statuses from detection on, in time order, as the live loop would see them."""
    m = event.month
    before = _looked_like_this(stack, m, event.pixels, cfg)
    if before:
        event.set("seasonal", f"looked like this in {before}", event.detected)
        return
    event.set("pending", seen_on, event.detected)

    for month in stack.months[stack.index(m) :]:
        reference = refs.get(month, m)  # history before the change
        if reference is None:
            continue
        for o in passes.in_month(month):
            if o.acquired <= event.detected:
                continue
            share = agreement(detector, reference, o.values, event.pixels, cfg)
            if share is not None:
                event.shares.append(share)
                confirmed = share >= cfg.agree_frac
                event.set("confirmed" if confirmed else "dismissed", o.item_id, o.acquired)
                break
        if event.status != "pending":
            break
    if event.status != "confirmed":
        return

    for j in range(1, cfg.seasonal_revert_months + 1):
        month = shift_month(m, j)
        values, reference = stack.at(month), refs.get(month, m)
        if values is None:
            break
        share = (
            None if reference is None else agreement(detector, reference, values, event.pixels, cfg)
        )
        if share is not None and share < cfg.agree_frac:
            obs = passes.in_month(month)
            at = obs[-1].acquired if obs else np.datetime64(month, "D")
            event.set("seasonal", f"reverted by {month}", at)
            return


def detect(
    stack: Stack, passes: Passes, grid: AOIGrid, method: Method, cfg: DetectionConfig
) -> list[Event]:
    """Every change in the stack, once, in the month it first shows."""
    refs = _References(stack, method.detector)
    events: list[Event] = []
    claimed_until = np.full((grid.height, grid.width), -1)  # month index a pixel is spoken for
    for i, month in enumerate(stack.months):
        reference = refs.get(month, month)
        if reference is None:
            continue
        changed = method.detector.changed(reference, stack.at(month)) & (claimed_until < i)
        for pixels in change_regions(changed, grid, cfg.min_mapping_unit_ha):
            if method.rules:
                seen = _first_showing(month, pixels, passes, refs, method.detector, cfg)
                if seen is None:
                    continue  # no single clear pass shows it: a median artefact
            else:
                seen = _first_clear(month, pixels, passes, cfg)
            before_month = shift_month(month, -YEAR)
            b, a = _median(stack.at(before_month), pixels), _median(stack.at(month), pixels)
            event = Event(
                method=method.name,
                month=month,
                reference_month=before_month,
                pixels=pixels,
                footprint=footprint(pixels, grid),
                category=categorize(b, a, cfg.categories),
                ndvi_before=round(float(_ndvi(b)), 3),
                ndvi_after=round(float(_ndvi(a)), 3),
                detected=seen[1],
                items=[
                    o.item_id
                    for m in (before_month, month)
                    for o in passes.in_month(m)
                    if observed_share(pixels, o.values)
                ],
            )
            if method.rules:
                event.shares.append(seen[2])
                snowy = _snow_month(stack, (before_month, month), pixels, cfg)
                if snowy:
                    event.set("seasonal", f"snow in {snowy}", event.detected)
                    events.append(event)
                    continue  # it claims nothing: a change under the snow shows once it melts
                _follow(event, seen[0], stack, passes, refs, method.detector, cfg)
            events.append(event)
            # The edges flicker with threshold noise and misregistration: keep them too.
            ring = ndimage.binary_dilation(pixels, iterations=CLAIM_RING_PX)
            claimed_until[ring] = i + YEAR - 1
    return events
