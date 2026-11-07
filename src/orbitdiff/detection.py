"""Change detection: from an AOI's monthly composites to change events.

Detectors compare like with like: a month against the same month a year earlier
(anniversary composites). Only pixels clearly observed on both sides can change, so gaps,
clouds and swath edges never do, and gap-filled display composites never reach this
module. Changed pixels lose 1-2 pixel misregistration slivers to a morphological opening,
group into regions, and regions under the minimum mapping unit are dropped. Footprints are
polygons in the AOI's UTM zone.

A change shows against its anniversary for a year after it happens, until the reference
catches up, so a pixel belongs to at most one event per year.

Every event records where it came from: the months compared, the passes behind them and
the method that found it. `detect` is the one path for the backfill, new passes and the
evaluation set.
"""

from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

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
YEAR = 12  # months
CACHED_MONTHS = 2 * YEAR + 2  # pass months kept in memory: a reference year and a margin

# (before, after) -> changed pixels (y, x). Both are (band, y, x) reflectance, NaN where
# not clearly observed; comparisons with NaN are False, so missing never means changed.
Detector = Callable[[np.ndarray, np.ndarray], np.ndarray]


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

    def at(self, month: str) -> np.ndarray | None:
        i = month_gap(self.months[0], month) if self.months else -1
        return self.values[i] if 0 <= i < len(self.months) else None


def load_stack(data_dir: Path, aoi_id: str, first: str = "", last: str = "9999-12") -> Stack:
    """The detection composites between `first` and `last`, inclusive."""
    months = tuple(
        m.month for m in composites.read_months(data_dir, aoi_id) if first <= m.month <= last
    )
    folder = composites.composite_dir(data_dir, aoi_id)
    values = [composites.read_composite(folder / f"{m}.tif") for m in months]
    return Stack(months, np.stack(values) if values else np.empty((0, len(BANDS), 0, 0)))


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


def ndvi_drop(threshold: float) -> Detector:
    """The naive baseline: NDVI fell by more than `threshold`. Sees vegetation loss only."""

    def changed(before: np.ndarray, after: np.ndarray) -> np.ndarray:
        return _ndvi(before) - _ndvi(after) > threshold

    return changed


@dataclass(frozen=True)
class Method:
    """A detector plus the rules applied to what it finds."""

    name: str
    detector: Detector


def methods(cfg: DetectionConfig) -> dict[str, Method]:
    return {"naive": Method("naive", ndvi_drop(cfg.ndvi_drop))}


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
    reference_month: str  # what that month was compared with: the same month a year earlier
    pixels: np.ndarray  # (y, x) footprint on the AOI's grid
    footprint: BaseGeometry  # the same, as a polygon in the AOI's UTM zone
    category: str
    ndvi_before: float  # medians over the footprint
    ndvi_after: float
    detected: np.datetime64  # the first pass in `month` that saw the footprint clearly
    items: list[str] = field(default_factory=list)  # passes clear over it in both months

    @property
    def area_ha(self) -> float:
        return round(float(self.pixels.sum() * PIXEL_HA), 2)


def observed_share(pixels: np.ndarray, *layers: np.ndarray) -> float:
    """Share of the footprint observed in every one of `layers` (band, y, x)."""
    seen = pixels.copy()
    for layer in layers:
        seen &= ~np.isnan(layer[0])
    return float(seen.sum() / pixels.sum())


def _first_seen(month: str, pixels: np.ndarray, passes: Passes, cfg: DetectionConfig):
    """The first pass in `month` clear over enough of the footprint; the month's last pass
    (the composite's latest input) when none is; the month's start when it has none."""
    obs = passes.in_month(month)
    for o in obs:
        if observed_share(pixels, o.values) >= cfg.footprint_clear_frac:
            return o.acquired
    return obs[-1].acquired if obs else np.datetime64(month, "D")


def _provenance(months: tuple[str, ...], pixels: np.ndarray, passes: Passes) -> list[str]:
    return [
        o.item_id for m in months for o in passes.in_month(m) if observed_share(pixels, o.values)
    ]


def detect(
    stack: Stack, passes: Passes, grid: AOIGrid, method: Method, cfg: DetectionConfig
) -> list[Event]:
    """Every change in the stack, once, in the month it first shows against its anniversary."""
    events: list[Event] = []
    claimed_until = np.full((grid.height, grid.width), -1)  # month index a pixel is spoken for
    for i, month in enumerate(stack.months):
        reference = shift_month(month, -YEAR)
        before, after = stack.at(reference), stack.values[i]
        if before is None:
            continue
        changed = method.detector(before, after) & (claimed_until < i)
        for pixels in change_regions(changed, grid, cfg.min_mapping_unit_ha):
            b, a = np.nanmedian(before[:, pixels], axis=1), np.nanmedian(after[:, pixels], axis=1)
            events.append(
                Event(
                    method=method.name,
                    month=month,
                    reference_month=reference,
                    pixels=pixels,
                    footprint=footprint(pixels, grid),
                    category=categorize(b, a, cfg.categories),
                    ndvi_before=round(float(_ndvi(b)), 3),
                    ndvi_after=round(float(_ndvi(a)), 3),
                    detected=_first_seen(month, pixels, passes, cfg),
                    items=_provenance((reference, month), pixels, passes),
                )
            )
            claimed_until[pixels] = i + YEAR - 1
    return events
