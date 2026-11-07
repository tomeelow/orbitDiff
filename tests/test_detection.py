"""Detection on a small synthetic landscape with staged changes, as in the mockup: forest
and fields, one clearing, a misregistration sliver, a speck and a cloud gap."""

import numpy as np
import pytest

from orbitdiff.config import pipeline_config
from orbitdiff.detection import (
    PIXEL_HA,
    Observation,
    Stack,
    categorize,
    change_regions,
    detect,
    methods,
    shift_month,
)

FOREST = np.array([0.02, 0.05, 0.03, 0.35])  # blue, green, red, nir: NDVI 0.84
BARE = np.array([0.08, 0.10, 0.14, 0.20])  # NDVI 0.18, brighter in the visible
CLEARING = (slice(20, 40), slice(30, 50))  # 20 x 20 px = 4 ha
MONTHS = tuple(str(m) for m in np.arange(np.datetime64("2020-01"), np.datetime64("2022-01")))


@pytest.fixture
def cfg():
    return pipeline_config().detection


class FakePasses:
    """Two passes a month, on the 5th and the 20th, each showing that month's composite."""

    def __init__(self, stack: Stack, hide: dict[str, np.ndarray] | None = None):
        self.stack, self.hide = stack, hide or {}

    def in_month(self, month: str) -> list[Observation]:
        values = self.stack.at(month)
        if values is None:
            return []
        first = values.copy()
        if month in self.hide:  # cloud over these pixels on the first pass
            first[:, self.hide[month]] = np.nan
        day = np.datetime64(month, "D")
        return [
            Observation(f"{month}-a", day + np.timedelta64(4, "D"), first),
            Observation(f"{month}-b", day + np.timedelta64(19, "D"), values),
        ]


def landscape(grid, seed: int = 0) -> np.ndarray:
    """(month, band, y, x): forest with a little noise from month to month."""
    rng = np.random.default_rng(seed)
    shape = (len(MONTHS), 4, grid.height, grid.width)
    return (FOREST[None, :, None, None] + rng.normal(0, 0.004, shape)).astype(np.float32)


def stage(values: np.ndarray, month: str, where, bands: np.ndarray) -> None:
    """From `month` on, `where` looks like `bands`."""
    values[MONTHS.index(month) :, :, *where] = bands[:, None, None]


def test_a_clearing_is_one_vegetation_loss_event_in_the_month_it_shows(grid, cfg):
    values = landscape(grid)
    stage(values, "2021-05", CLEARING, BARE)
    stack = Stack(MONTHS, values)

    events = detect(stack, FakePasses(stack), grid, methods(cfg)["naive"], cfg)

    assert len(events) == 1  # not again for a year: the reference hasn't caught up yet
    e = events[0]
    assert (e.month, e.reference_month) == ("2021-05", "2020-05")
    assert e.category == "vegetation_loss"
    assert e.area_ha == pytest.approx(20 * 20 * PIXEL_HA)
    assert e.footprint.area == pytest.approx(20 * 20 * 100)  # m², in the AOI's UTM zone
    assert e.ndvi_before > 0.8 > 0.2 > e.ndvi_after
    assert e.detected == np.datetime64("2021-05-05")
    assert e.items == ["2020-05-a", "2020-05-b", "2021-05-a", "2021-05-b"]


def test_detection_waits_for_a_pass_that_sees_the_footprint(grid, cfg):
    values = landscape(grid)
    stage(values, "2021-05", CLEARING, BARE)
    stack = Stack(MONTHS, values)
    cloud = np.zeros((grid.height, grid.width), bool)
    cloud[CLEARING] = True

    (e,) = detect(stack, FakePasses(stack, {"2021-05": cloud}), grid, methods(cfg)["naive"], cfg)

    assert e.detected == np.datetime64("2021-05-20")
    assert "2021-05-a" not in e.items  # it saw none of the footprint


def test_slivers_specks_and_missing_pixels_are_not_change(grid, cfg):
    values = landscape(grid)
    stage(values, "2021-05", (slice(60, 62), slice(10, 120)), BARE)  # 2 px misregistration
    stage(values, "2021-05", (slice(80, 83), slice(80, 83)), BARE)  # 0.09 ha speck
    values[MONTHS.index("2021-03"), :, :, : grid.width // 2] = np.nan  # cloud gap
    values[MONTHS.index("2020-03"), :, :, grid.width // 2 :] = np.nan  # gap in the reference
    stack = Stack(MONTHS, values)

    assert detect(stack, FakePasses(stack), grid, methods(cfg)["naive"], cfg) == []


def test_regions_keep_three_pixel_wide_strips_above_the_mapping_unit(grid):
    changed = np.zeros((grid.height, grid.width), bool)
    changed[50:53, 10:60] = True  # 3 px wide, 1.5 ha: a new road survives the opening
    (region,) = change_regions(changed, grid, min_area_ha=0.1)
    assert region.sum() == 150


@pytest.mark.parametrize(
    ("before", "after", "category"),
    [
        (FOREST, BARE, "vegetation_loss"),
        ([0.06, 0.08, 0.07, 0.25], [0.12, 0.13, 0.14, 0.18], "buildings_roads"),  # field to roofs
        ([0.06, 0.08, 0.07, 0.25], [0.03, 0.04, 0.05, 0.08], "solar_industry"),  # dark all year
        ([0.06, 0.08, 0.07, 0.25], [0.05, 0.06, 0.04, 0.02], "water"),
        (FOREST, [0.03, 0.06, 0.05, 0.30], "other"),  # thinning, still green
    ],
)
def test_categories_follow_the_plan_heuristics(cfg, before, after, category):
    assert categorize(np.array(before), np.array(after), cfg.categories) == category


def test_shift_month_crosses_years():
    assert shift_month("2021-02", -12) == "2020-02"
    assert shift_month("2021-12", 1) == "2022-01"
