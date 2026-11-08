"""A cross-check of vegetation-loss events against Hansen Global Forest Change (GFC).

For `orbitdiff evaluate` only: nothing in detection, alerting or promotion reads it. It
sits next to the labelled evaluation and doesn't replace it. GFC is another model's annual
map from Landsat, with errors of its own and a lag of months after each year ends, so
agreement with it is a second opinion and disagreement is a place to look.

GFC is 0.00025 degrees (nominally 30 m) and annual; events are 10 m and monthly. So its
layers are read onto the AOI's own grid by nearest neighbour, since loss years are codes,
and years are compared with a tolerance: an event's month is the later of an anniversary
pair, so the loss can be from the year before, and GFC dates a loss by when Landsat could
first see it.

Source: Hansen/UMD/Google/USGS/NASA (CC BY 4.0; full citation in the README).
"""

import math
from collections.abc import Iterable
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import TYPE_CHECKING

import numpy as np
from loguru import logger
from rasterio.errors import RasterioIOError
from rasterio.warp import transform_bounds

from orbitdiff.aoi import AOIGrid
from orbitdiff.config import GfcConfig, PipelineConfig
from orbitdiff.detection import Event, change_regions
from orbitdiff.reading import read_band

if TYPE_CHECKING:
    from orbitdiff.evaluation import EvalAoi

LOSS_EPOCH = 2000  # lossyear's codes count from here: 1 is 2001
VEGETATION_LOSS = "vegetation_loss"
STANDING = {"pending", "confirmed"}  # events the pipeline hasn't itself thrown out
CAVEAT = (
    "A second opinion, not ground truth and not a score: GFC is another model's annual "
    "Landsat map, with errors of its own and a lag after each year ends. It can only speak "
    "for stand-replacing loss of trees that stood in 2000. Disagreement is a place to look."
)


def granules(west: float, south: float, east: float, north: float) -> list[str]:
    """GFC's 10-degree granules over a lon/lat box, named for their top-left corners."""
    tops = range(math.floor(south / 10) * 10 + 10, math.ceil(north / 10) * 10 + 1, 10)
    lefts = range(math.floor(west / 10) * 10, math.ceil(east / 10) * 10, 10)
    return [
        f"{abs(top):02d}{'N' if top >= 0 else 'S'}_{abs(left):03d}{'E' if left >= 0 else 'W'}"
        for top in tops
        for left in lefts
    ]


@dataclass(frozen=True)
class Layers:
    """GFC as published, on one AOI's grid."""

    lossyear: np.ndarray  # (y, x): 0 no loss, else years since LOSS_EPOCH
    treecover: np.ndarray  # (y, x): percent canopy in 2000
    bytes_read: int  # compressed bytes of the blocks under the window, both layers
    full_bytes: int  # compressed bytes of the granules those blocks are in


def read_layers(grid: AOIGrid, cfg: GfcConfig, data_dir: Path) -> Layers:
    """GFC over the AOI, reading only its window. Kept per grid and raw, so changing a
    threshold here reads nothing again."""
    key = f"{grid.aoi_id}_{grid.epsg}_{grid.transform.c:.0f}_{grid.transform.f:.0f}"
    path = data_dir / "gfc" / cfg.version / f"{key}_{grid.width}x{grid.height}.npz"
    if path.exists():
        with np.load(path) as saved:
            return Layers(
                saved["lossyear"],
                saved["treecover"],
                int(saved["bytes_read"]),
                int(saved["full_bytes"]),
            )
    names = granules(*transform_bounds(grid.crs, "EPSG:4326", *grid.bounds, densify_pts=21))
    reads = {
        layer: [
            read_band(
                cfg.url.format(version=cfg.version, layer=layer, granule=name),
                grid,
                categorical=True,
            )
            for name in names
        ]
        for layer in ("lossyear", "treecover2000")
    }
    every_read = [r for layer_reads in reads.values() for r in layer_reads]
    layers = Layers(
        # Each granule reads 0 outside itself, so an AOI across a seam is their maximum.
        np.maximum.reduce([r.data for r in reads["lossyear"]]),
        np.maximum.reduce([r.data for r in reads["treecover2000"]]),
        sum(r.bytes_read for r in every_read),
        sum(r.full_bytes for r in every_read),
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(path, **asdict(layers))
    logger.info(
        "GFC {} for AOI {}: granules {}, read {} of {} bytes",
        cfg.version,
        grid.aoi_id,
        names,
        layers.bytes_read,
        layers.full_bytes,
    )
    return layers


def judged_years(first: str, last: str, last_loss_year: int) -> range:
    """The calendar years that the period covers whole and GFC has mapped: a loss in a part
    year may fall outside the imagery."""
    start = int(first[:4]) + (first[5:] != "01")
    end = min(int(last[:4]) - (last[5:] != "12"), last_loss_year)
    return range(start, end + 1)


@dataclass
class Tally:
    """One method's comparison over one area, or summed over several."""

    events: int = 0  # vegetation-loss events
    matched: int = 0  # enough of the footprint is GFC loss in the same or an adjacent year
    no_loss: int = 0  # GFC could have seen it, in forest and in its years, and has no loss
    not_forest: int = 0  # too little of it had trees in 2000: GFC can't speak for it
    after_gfc: int = 0  # later than GFC's last year: GFC can't speak for it yet
    patches: int = 0  # GFC loss patches, above the minimum mapping unit, in the judged years
    patches_matched: int = 0  # covered by vegetation-loss events
    patches_other_category: int = 0  # covered, but only by events of another category
    patches_missed: int = 0  # covered by no event

    def add(self, other: "Tally") -> None:
        for f in fields(self):
            setattr(self, f.name, getattr(self, f.name) + getattr(other, f.name))

    @property
    def matched_share(self) -> float | None:
        judged = self.matched + self.no_loss
        return self.matched / judged if judged else None

    @property
    def patches_matched_share(self) -> float | None:
        return self.patches_matched / self.patches if self.patches else None


def _year(event: Event) -> int:
    return int(event.month[:4])


def _union(events: list[Event], shape: tuple[int, int]) -> np.ndarray:
    union = np.zeros(shape, bool)
    for event in events:
        union |= event.pixels
    return union


def compare(
    events: Iterable[Event],
    layers: Layers,
    grid: AOIGrid,
    years: range,
    cfg: GfcConfig,
    min_area_ha: float,
) -> Tally:
    """Events against GFC's loss, and GFC's loss against events, on the AOI's grid."""
    events = list(events)
    forest = layers.treecover >= cfg.min_treecover
    loss_year = np.where(
        forest & (layers.lossyear > 0), layers.lossyear.astype(np.int32) + LOSS_EPOCH, 0
    )
    tally = Tally()
    for event in (e for e in events if e.category == VEGETATION_LOSS):
        tally.events += 1
        year = _year(event)
        loss_share = (np.abs(loss_year - year) <= cfg.year_tolerance)[event.pixels].mean()
        if year > cfg.last_loss_year:
            tally.after_gfc += 1
        elif loss_share >= cfg.min_overlap:
            tally.matched += 1
        elif forest[event.pixels].mean() < cfg.min_overlap:
            tally.not_forest += 1
        else:
            tally.no_loss += 1

    shape = (grid.height, grid.width)
    for year in years:
        # The same regions events are made of: slivers go, the minimum mapping unit applies.
        patches = change_regions(loss_year == year, grid, min_area_ha)
        near = [e for e in events if abs(_year(e) - year) <= cfg.year_tolerance]
        vegetation = _union([e for e in near if e.category == VEGETATION_LOSS], shape)
        anything = _union(near, shape)
        for patch in patches:
            tally.patches += 1
            if vegetation[patch].mean() >= cfg.min_overlap:
                tally.patches_matched += 1
            elif anything[patch].mean() >= cfg.min_overlap:
                tally.patches_other_category += 1
            else:
                tally.patches_missed += 1
    return tally


def _count(part: int, share: float | None) -> str:
    return str(part) if share is None else f"{part} ({share:.0%})"


class CrossCheck:
    """The comparison per method and area, collected while `orbitdiff evaluate` runs."""

    def __init__(self, cfg: PipelineConfig, data_dir: Path) -> None:
        self._cfg = cfg.evaluation.gfc
        self._min_area_ha = cfg.detection.min_mapping_unit_ha
        self._data_dir = data_dir
        self._layers: dict[str, Layers | None] = {}  # by area; None: it couldn't be read
        self._tallies: dict[str, dict[str, Tally]] = {}  # method -> area -> tally

    def _layers_of(self, area: str, grid: AOIGrid) -> Layers | None:
        if area not in self._layers:
            try:
                self._layers[area] = read_layers(grid, self._cfg, self._data_dir)
            except RasterioIOError as exc:  # never at the cost of the labelled evaluation
                logger.warning("GFC could not be read for {}: {}", area, exc)
                self._layers[area] = None
        return self._layers[area]

    def add(
        self, row: str, aoi: "EvalAoi", grid: AOIGrid, events: Iterable[Event], confirmed_only: bool
    ) -> None:
        layers = self._layers_of(aoi.name, grid)
        if layers is None:
            return
        kept = {"confirmed"} if confirmed_only else STANDING
        self._tallies.setdefault(row, {})[aoi.name] = compare(
            (e for e in events if e.status in kept),
            layers,
            grid,
            judged_years(aoi.first, aoi.last, self._cfg.last_loss_year),
            self._cfg,
            self._min_area_ha,
        )

    def _overall(self, row: str) -> Tally:
        total = Tally()
        for tally in self._tallies.get(row, {}).values():
            total.add(tally)
        return total

    def table(self) -> str:
        """The comparison, per area and overall, as Markdown, with what it can't tell you."""
        last = self._cfg.last_loss_year
        lines = [
            f"GFC cross-check ({self._cfg.version}, Source: Hansen/UMD/Google/USGS/NASA). {CAVEAT}",
            "",
            f"| Method | Area | Vegetation-loss events | Matched, within "
            f"{self._cfg.year_tolerance} yr | No GFC loss | Not forest in 2000 | After {last} "
            f"| GFC patches | Seen as vegetation loss | Seen as other category "
            f"| Seen by no event |",
            "|---|---|---|---|---|---|---|---|---|---|---|",
        ]
        for row, areas in self._tallies.items():
            for area, t in (*areas.items(), ("all areas", self._overall(row))):
                lines.append(
                    f"| {row} | {area} | {t.events} | {_count(t.matched, t.matched_share)} "
                    f"| {t.no_loss} | {t.not_forest} | {t.after_gfc} | {t.patches} "
                    f"| {_count(t.patches_matched, t.patches_matched_share)} "
                    f"| {t.patches_other_category} | {t.patches_missed} |"
                )
        read = {a: layers for a, layers in self._layers.items() if layers is not None}
        lines += [""] + [
            f"Read {layers.bytes_read / 1e6:.2f} MB of {layers.full_bytes / 1e6:.0f} MB for {area}."
            for area, layers in read.items()
        ]
        lines += [f"GFC could not be read for {a}." for a in sorted(set(self._layers) - set(read))]
        return "\n".join(lines)

    def params(self) -> dict[str, object]:
        """For MLflow, next to the run's thresholds."""
        return {f"gfc_{k}": v for k, v in self._cfg.model_dump(exclude={"url"}).items()}

    def metrics(self, row: str) -> dict[str, float]:
        """For MLflow, all named gfc_crosscheck_: no one should read these as labelled scores."""
        areas = self._tallies.get(row)
        if not areas:
            return {}
        out = {"gfc_crosscheck_bytes_read": float(sum(self._layers[a].bytes_read for a in areas))}
        for area, t in (("", self._overall(row)), *areas.items()):
            stem = f"gfc_crosscheck_{area}_" if area else "gfc_crosscheck_"
            out |= {f"{stem}{k}": float(v) for k, v in asdict(t).items()}
            for name in ("matched_share", "patches_matched_share"):
                value = getattr(t, name)
                if value is not None:
                    out[f"{stem}{name}"] = value
        return out
