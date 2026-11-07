"""Detection on small synthetic landscapes with staged changes, as in the mockup: forest,
fields and water, a clearing, a building, floods, slivers, specks and cloud gaps."""

import hashlib
import json

import numpy as np
import pytest

from orbitdiff.config import pipeline_config
from orbitdiff.detection import (
    PIXEL_HA,
    PRODUCTION,
    RULES_REVISION,
    Learned,
    Observation,
    Stack,
    categorize,
    change_regions,
    detect,
    methods,
    shift_month,
    thresholds,
    version,
)

FOREST = np.array([0.02, 0.05, 0.03, 0.35])  # blue, green, red, nir: NDVI 0.84
BARE = np.array([0.08, 0.10, 0.14, 0.20])  # NDVI 0.18, brighter in the visible
FIELD = np.array([0.06, 0.08, 0.07, 0.25])  # NDVI 0.56
ROOFS = np.array([0.15, 0.16, 0.17, 0.30])  # NDVI 0.28: a drop of 0.28 from the field
WATER = np.array([0.05, 0.06, 0.04, 0.02])
ROOFS_BRIGHT = np.array([0.25, 0.26, 0.27, 0.30])  # NDVI 0.05, bright in every band
SNOW = np.array([0.80, 0.80, 0.78, 0.74])  # bright everywhere, NDVI about -0.03
CLEARING = (slice(20, 40), slice(30, 50))  # 20 x 20 px = 4 ha
FLOODPLAIN = (slice(90, 110), slice(100, 140))
MONTHS = tuple(str(m) for m in np.arange(np.datetime64("2019-01"), np.datetime64("2022-07")))


@pytest.fixture
def cfg():
    return pipeline_config().detection


class FakePasses:
    """Two passes a month, on the 5th ("a") and the 20th ("b"), each showing that month's
    composite unless `show` says otherwise for (month, "a" | "b")."""

    def __init__(self, stack: Stack, show: dict | None = None):
        self.stack, self.show = stack, show or {}

    def in_month(self, month: str) -> list[Observation]:
        values = self.stack.at(month)
        if values is None:
            return []
        day = np.datetime64(month, "D")
        return [
            Observation(
                f"{month}-{name}",
                day + np.timedelta64(offset, "D"),
                self.show.get((month, name), values),
            )
            for name, offset in (("a", 4), ("b", 19))
        ]


def landscape(grid, months=MONTHS, base=FOREST, seed: int = 0) -> np.ndarray:
    """(month, band, y, x): `base` everywhere, with a little noise from month to month."""
    rng = np.random.default_rng(seed)
    shape = (len(months), 4, grid.height, grid.width)
    return (base[None, :, None, None] + rng.normal(0, 0.004, shape)).astype(np.float32)


def stage(values, where, bands, first: str, last: str = MONTHS[-1], months=MONTHS) -> None:
    """From `first` to `last`, `where` looks like `bands`."""
    values[months.index(first) : months.index(last) + 1, :, *where] = bands[:, None, None]


def looks(grid, where, bands, elsewhere=FOREST) -> np.ndarray:
    """One observation: `elsewhere` everywhere, `bands` at `where`."""
    out = np.broadcast_to(elsewhere[:, None, None], (4, grid.height, grid.width)).copy()
    out[:, *where] = bands[:, None, None]
    return out.astype(np.float32)


def run(grid, cfg, values, method="naive", show=None, snow=None):
    return run_with(grid, cfg, values, methods(cfg)[method], show, snow)


def run_with(grid, cfg, values, method, show=None, snow=None):
    stack = Stack(MONTHS, values, snow)
    return detect(stack, FakePasses(stack, show), grid, method, cfg)


# Without rules: what each detector sees


def test_a_clearing_is_one_vegetation_loss_event_in_the_month_it_shows(grid, cfg):
    values = landscape(grid)
    stage(values, CLEARING, BARE, "2021-05")

    events = run(grid, cfg, values)

    assert len(events) == 1  # not again for a year: the reference hasn't caught up yet
    e = events[0]
    assert (e.month, e.reference_month) == ("2021-05", "2020-05")
    assert e.category == "vegetation_loss"
    assert e.area_ha == pytest.approx(20 * 20 * PIXEL_HA)
    assert e.footprint.area == pytest.approx(20 * 20 * 100)  # m², in the AOI's UTM zone
    assert e.ndvi_before > 0.8 > 0.2 > e.ndvi_after
    assert e.detected == np.datetime64("2021-05-05")
    assert e.items == ["2020-05-a", "2020-05-b", "2021-05-a", "2021-05-b"]
    assert e.history == []  # no rules, no statuses


def test_detection_waits_for_a_pass_that_sees_the_footprint(grid, cfg):
    values = landscape(grid)
    stage(values, CLEARING, BARE, "2021-05")
    cloudy = looks(grid, CLEARING, np.full(4, np.nan))

    (e,) = run(grid, cfg, values, show={("2021-05", "a"): cloudy})

    assert e.detected == np.datetime64("2021-05-20")
    assert "2021-05-a" not in e.items  # it saw none of the footprint


def test_slivers_specks_and_missing_pixels_are_not_change(grid, cfg):
    values = landscape(grid)
    stage(values, (slice(60, 62), slice(10, 120)), BARE, "2021-05")  # 2 px misregistration
    stage(values, (slice(80, 83), slice(80, 83)), BARE, "2021-05")  # 0.09 ha speck
    values[MONTHS.index("2021-03"), :, :, : grid.width // 2] = np.nan  # cloud gap
    values[MONTHS.index("2020-03"), :, :, grid.width // 2 :] = np.nan  # gap in the reference

    assert run(grid, cfg, values, PRODUCTION) == []


def test_regions_keep_three_pixel_wide_strips_above_the_mapping_unit(grid):
    changed = np.zeros((grid.height, grid.width), bool)
    changed[50:53, 10:60] = True  # 3 px wide, 1.5 ha: a new road survives the opening
    (region,) = change_regions(changed, grid, min_area_ha=0.1)
    assert region.sum() == 150


def test_cva_sees_roofs_on_a_field_that_the_naive_baseline_misses(grid, cfg):
    values = landscape(grid, base=FIELD)
    stage(values, CLEARING, ROOFS, "2021-05")

    assert run(grid, cfg, values, "naive") == []
    (e,) = run(grid, cfg, values, "cva")
    assert (e.month, e.category) == ("2021-05", "other")  # NDVI 0.28 is above the built cut


def test_the_seasonal_model_flags_a_break_but_not_the_crop_cycle(grid, cfg):
    months = tuple(str(m) for m in np.arange(np.datetime64("2018-01"), np.datetime64("2023-01")))
    rng = np.random.default_rng(1)
    t = np.arange(len(months))
    # A field's NDVI swings 0.2-0.8 every year, its timing a little different each year.
    ndvi = 0.5 + 0.3 * np.sin(2 * np.pi * (t - 3 + rng.integers(-1, 2, len(t)) * 0.3) / 12)
    ndvi = ndvi[:, None, None] + rng.normal(0, 0.02, (len(t), grid.height, grid.width))
    nir = np.full_like(ndvi, 0.3)
    values = np.stack(
        [np.full_like(ndvi, 0.03), np.full_like(ndvi, 0.06), nir * (1 - ndvi) / (1 + ndvi), nir],
        axis=1,
    ).astype(np.float32)
    stage(values, CLEARING, BARE, "2021-06", months[-1], months)  # a building site from June
    stack = Stack(months, values)

    events = detect(stack, FakePasses(stack), grid, methods(cfg)["bfast"], cfg)

    assert [e.month for e in events] == ["2021-06"]
    assert events[0].pixels.sum() == 400


# With rules: the production method and its statuses


def test_a_change_the_next_clear_pass_agrees_with_is_confirmed(grid, cfg):
    values = landscape(grid)
    stage(values, CLEARING, BARE, "2021-05")

    (e,) = run(grid, cfg, values, PRODUCTION)

    assert e.status == "confirmed"
    assert e.history == [
        ("pending", "2021-05-a", np.datetime64("2021-05-05")),
        ("confirmed", "2021-05-b", np.datetime64("2021-05-20")),
    ]


def test_a_change_the_next_clear_pass_contradicts_is_dismissed(grid, cfg):
    values = landscape(grid)
    stage(values, CLEARING, BARE, "2021-05", "2021-05")  # the composite and one pass show it
    forest = looks(grid, CLEARING, FOREST)

    e = run(grid, cfg, values, PRODUCTION, show={("2021-05", "b"): forest})[0]

    assert (e.month, e.status) == ("2021-05", "dismissed")
    assert e.history[-1] == ("dismissed", "2021-05-b", np.datetime64("2021-05-20"))


def test_a_change_no_single_pass_shows_is_not_an_event(grid, cfg):
    values = landscape(grid)
    stage(values, CLEARING, BARE, "2021-05", "2021-05")  # in the composite only
    forest = looks(grid, CLEARING, FOREST)
    show = {("2021-05", "a"): forest, ("2021-05", "b"): forest}

    assert [e.month for e in run(grid, cfg, values, "naive", show=show)] == ["2021-05"]
    (later,) = run(grid, cfg, values, PRODUCTION, show=show)
    # Not in May 2021. A year on, May 2022 differs from that odd composite, but looks as
    # May 2020 did: seasonal from the start, so it never alerts.
    assert later.month == "2022-05"
    assert later.history == [("seasonal", "looked like this in 2020-05", later.detected)]


def test_a_flood_that_recedes_is_seasonal_and_next_years_flood_is_seasonal_from_the_start(
    grid, cfg
):
    values = landscape(grid)
    stage(values, FLOODPLAIN, WATER, "2020-04", "2020-04")
    stage(values, FLOODPLAIN, WATER, "2021-04", "2021-04")

    first, second, dry = run(grid, cfg, values, PRODUCTION)

    assert (first.month, first.category) == ("2020-04", "water")
    assert [s for s, _, _ in first.history] == ["pending", "confirmed", "seasonal"]
    assert first.history[-1][1] == "reverted by 2020-05"
    assert second.month == "2021-04"
    assert second.history == [("seasonal", "looked like this in 2020-04", second.detected)]
    # A dry April after a flooded one differs too, and looks as April 2019 did.
    assert (dry.month, dry.category) == ("2022-04", "other")
    assert dry.history == [("seasonal", "looked like this in 2019-04", dry.detected)]


def test_a_field_in_a_crop_rotation_is_seasonal(grid, cfg):
    values = landscape(grid, base=FIELD)
    for year in ("2020", "2022"):  # bare every other spring
        stage(values, CLEARING, BARE, f"{year}-03", f"{year}-04")

    first, again = run(grid, cfg, values, PRODUCTION)

    # The archive has never seen it bare in spring 2020: a change, until it greens by May.
    assert first.month == "2020-03"
    assert [s for s, _, _ in first.history] == ["pending", "confirmed", "seasonal"]
    # Bare again in 2022, as in 2020: seasonal from the start, never an alert.
    assert again.month == "2022-03"
    assert again.history == [("seasonal", "looked like this in 2020-03", again.detected)]


def test_a_building_on_last_years_clearing_is_a_new_change_not_a_season(grid, cfg):
    values = landscape(grid)
    stage(values, CLEARING, BARE, "2020-05")
    stage(values, CLEARING, ROOFS_BRIGHT, "2021-05")

    clearing, building = run(grid, cfg, values, PRODUCTION)

    assert (clearing.month, clearing.category, clearing.status) == (
        "2020-05",
        "vegetation_loss",
        "confirmed",
    )
    assert (building.month, building.category, building.status) == (
        "2021-05",
        "buildings_roads",
        "confirmed",
    )


def test_snow_is_seasonal_and_hides_a_clearing_only_until_it_melts(grid, cfg):
    values = landscape(grid)
    stage(values, CLEARING, BARE, "2021-01")  # cleared in January, under snow that month
    stage(values, CLEARING, SNOW, "2021-01", "2021-01")
    snow = np.zeros((len(MONTHS), grid.height, grid.width), bool)
    snow[MONTHS.index("2021-01"), *CLEARING] = True

    under_snow, clearing = run(grid, cfg, values, PRODUCTION, snow=snow)

    assert under_snow.month == "2021-01"
    assert under_snow.history == [("seasonal", "snow in 2021-01", under_snow.detected)]
    assert (clearing.month, clearing.category, clearing.status) == (
        "2021-02",
        "vegetation_loss",
        "confirmed",
    )


class RoofModel:
    """A stand-in for a trained building model: P(change) 0.9 where the visible bands
    brightened by more than 0.05, 0.1 elsewhere, NaN where either image is missing."""

    def __init__(self, version: str = "orbitdiff_buildings/3"):
        self.version = version

    def __call__(self, before, after):
        brighter = (after[:3] - before[:3]).mean(axis=0) > 0.05
        missing = np.isnan(before[0]) | np.isnan(after[0])
        return np.where(missing, np.nan, np.where(brighter, 0.9, 0.1))


def test_a_building_model_finds_roofs_through_the_same_mapping_unit(grid, cfg):
    values = landscape(grid, base=FIELD)
    stage(values, CLEARING, ROOFS, "2021-05")
    values[MONTHS.index("2021-05") :, :, 60:62, 10:60] = ROOFS[:, None, None]  # a 2 px sliver
    values[MONTHS.index("2021-08") :, :, 90:93, 90:93] = ROOFS[:, None, None]  # one roof, 0.09 ha
    model = Learned(RoofModel(), threshold=0.5)

    (e,) = run_with(grid, cfg, values, methods(cfg, model)["unet"])

    assert (e.month, e.reference_month) == ("2021-05", "2020-05")
    assert e.area_ha == pytest.approx(20 * 20 * PIXEL_HA)


def test_a_learned_detector_asks_its_model_once_per_pair_of_images(grid):
    calls = []

    class Counting(RoofModel):
        def __call__(self, before, after):
            calls.append(1)
            return super().__call__(before, after)

    detector = Learned(Counting(), threshold=0.5)
    stack = Stack(MONTHS, landscape(grid))
    before, after = stack.at("2020-05"), stack.at("2021-05")
    assert before is stack.at("2020-05")  # one view per month, every time

    first = detector.changed(before, after)
    np.testing.assert_array_equal(detector.changed(before, after), first)
    assert len(calls) == 1
    detector.changed(before.copy(), after)  # same values, another array: asked again
    assert len(calls) == 2


def test_without_a_building_model_every_version_is_what_it_was(cfg):
    plain = methods(cfg)
    assert "unet" not in plain
    text = json.dumps([PRODUCTION, RULES_REVISION, cfg.model_dump()], sort_keys=True)
    digest = hashlib.sha256(text.encode()).hexdigest()[:8]
    assert version(plain[PRODUCTION], cfg) == f"production-{digest}"
    assert thresholds(plain[PRODUCTION], cfg) == cfg.model_dump()


def test_a_building_model_is_named_in_the_version_and_its_threshold_recorded(cfg):
    with_model = methods(cfg, Learned(RoofModel(), 0.5))[PRODUCTION]
    newer = methods(cfg, Learned(RoofModel("orbitdiff_buildings/4"), 0.5))[PRODUCTION]
    stricter = methods(cfg, Learned(RoofModel(), 0.7))[PRODUCTION]

    v = version(with_model, cfg)
    assert v.startswith("production-") and v.endswith("+orbitdiff_buildings/3")
    assert len(v) <= 48  # events.method_version
    assert len({v, version(newer, cfg), version(stricter, cfg)}) == 3
    assert v != version(methods(cfg)[PRODUCTION], cfg)
    assert thresholds(with_model, cfg) == cfg.model_dump() | {"buildings_threshold": 0.5}


def test_the_version_changes_with_any_threshold(cfg):
    method = methods(cfg)[PRODUCTION]
    tweaked = cfg.model_copy(update={"cva_magnitude": cfg.cva_magnitude + 0.01})
    assert version(method, cfg).startswith("production-")
    assert version(method, cfg) != version(method, tweaked)


@pytest.mark.parametrize(
    ("before", "after", "category"),
    [
        (FOREST, BARE, "vegetation_loss"),
        (FIELD, [0.12, 0.13, 0.14, 0.18], "buildings_roads"),  # field to roofs
        (FIELD, [0.03, 0.04, 0.05, 0.08], "solar_industry"),  # dark all year
        (FIELD, WATER, "water"),
        (FOREST, [0.03, 0.06, 0.05, 0.30], "other"),  # thinning, still green
    ],
)
def test_categories_follow_the_plan_heuristics(cfg, before, after, category):
    assert categorize(np.array(before), np.array(after), cfg.categories) == category


def test_shift_month_crosses_years():
    assert shift_month("2021-02", -12) == "2020-02"
    assert shift_month("2021-12", 1) == "2022-01"
