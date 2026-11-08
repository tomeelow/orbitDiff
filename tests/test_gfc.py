"""The GFC cross-check, on small synthetic rasters with staged loss years."""

import inspect
import re
from pathlib import Path

import numpy as np
import pytest
from helpers import write_tif
from rasterio.errors import RasterioIOError
from rasterio.transform import Affine
from shapely.geometry import Polygon, box

import orbitdiff
from orbitdiff import cli, gfc
from orbitdiff.aoi import make_grid
from orbitdiff.config import pipeline_config
from orbitdiff.detection import Event
from orbitdiff.evaluation import EvalAoi

PIPELINE = pipeline_config()
CFG = PIPELINE.evaluation.gfc
MMU = PIPELINE.detection.min_mapping_unit_ha
YEARS = range(2017, 2026)
PIXEL = 0.00025  # GFC's resolution, in degrees


def block(grid, y=40, x=40, height=10, width=10):
    pixels = np.zeros((grid.height, grid.width), bool)
    pixels[y : y + height, x : x + width] = True
    return pixels


def staged(grid, loss=(), treecover=80, bytes_read=0, full_bytes=0):
    """GFC on the grid, from (pixels, calendar year) pairs, coded as GFC publishes them."""
    lossyear = np.zeros((grid.height, grid.width), np.uint8)
    for pixels, year in loss:
        lossyear[pixels] = year - 2000  # 20 is 2020: written out, so the test pins the epoch
    cover = treecover if isinstance(treecover, np.ndarray) else np.full(lossyear.shape, treecover)
    return gfc.Layers(lossyear, cover.astype(np.uint8), bytes_read, full_bytes)


def event(pixels, month="2020-06", category=gfc.VEGETATION_LOSS, status="confirmed"):
    e = Event(
        "production",
        month,
        "2019-06",
        pixels,
        box(0, 0, 1, 1),
        category,
        0.8,
        0.2,
        np.datetime64(f"{month}-05"),
    )
    e.status = status
    return e


def site(name="site", first="2017-01", last="2026-11"):
    return EvalAoi(name, box(0, 0, 1, 1), first, last, (), False, ())


def compare(grid, events, layers, years=YEARS):
    return gfc.compare(events, layers, grid, years, CFG, MMU)


# Granules and the windowed read


@pytest.mark.parametrize(
    ("lonlat", "expected"),
    [
        ((13.775, 52.38, 13.82, 52.405), {"60N_010E"}),
        ((-59.1, -3.2, -58.9, -3.1), {"00N_060W"}),
        ((12.0, -10.5, 12.1, -10.4), {"10S_010E"}),
        ((10.0, 50.0, 10.1, 50.1), {"60N_010E"}),  # on a seam: only the granule it lies inside
        ((19.9, 49.9, 20.1, 50.1), {"50N_010E", "50N_020E", "60N_010E", "60N_020E"}),
    ],
)
def test_granules_are_named_for_their_top_left_corner(lonlat, expected):
    assert set(gfc.granules(*lonlat)) == expected


def granule(tmp_path, layer, name, data, west, north):
    transform = Affine(PIXEL, 0, west, 0, -PIXEL, north)
    write_tif(tmp_path / f"{CFG.version}_{layer}_{name}.tif", data, 4326, transform, block=32)


def local(tmp_path):
    return CFG.model_copy(update={"url": str(tmp_path / "{version}_{layer}_{granule}.tif")})


def test_only_the_window_is_read_and_loss_years_stay_codes(tmp_path, grid):
    codes = np.array([0, 17, 20, 25], np.uint8)  # 18 or 19 would be interpolation
    lossyear = np.random.default_rng(0).choice(codes, size=(1000, 1000))
    treecover = np.full((1000, 1000), 70, np.uint8)
    granule(tmp_path, "lossyear", "60N_030E", lossyear, 30.6, 50.45)
    granule(tmp_path, "treecover2000", "60N_030E", treecover, 30.6, 50.45)

    layers = gfc.read_layers(grid, local(tmp_path), tmp_path)

    assert layers.lossyear.shape == (grid.height, grid.width)
    assert set(np.unique(layers.lossyear)) <= set(codes.tolist())
    assert (layers.treecover[grid.mask] == 70).all()
    assert 0 < layers.bytes_read < layers.full_bytes


def test_an_area_is_read_once_per_grid(tmp_path, grid):
    for layer in ("lossyear", "treecover2000"):
        granule(tmp_path, layer, "60N_030E", np.full((1000, 1000), 20, np.uint8), 30.6, 50.45)
    first = gfc.read_layers(grid, local(tmp_path), tmp_path)
    for tif in tmp_path.glob("*.tif"):
        tif.unlink()

    again = gfc.read_layers(grid, local(tmp_path), tmp_path)

    np.testing.assert_array_equal(first.lossyear, again.lossyear)
    assert again.bytes_read == first.bytes_read


def test_an_area_across_a_granule_seam_reads_both(tmp_path):
    seam = make_grid("seam", box(29.99, 50.38, 30.01, 50.39), max_km2=10)
    for layer in ("lossyear", "treecover2000"):
        granule(tmp_path, layer, "60N_020E", np.full((400, 400), 5, np.uint8), 29.9, 50.45)
        granule(tmp_path, layer, "60N_030E", np.full((400, 400), 9, np.uint8), 30.0, 50.45)

    layers = gfc.read_layers(seam, local(tmp_path), tmp_path)

    assert set(np.unique(layers.lossyear[seam.mask])) == {5, 9}


# Which years can be judged


@pytest.mark.parametrize(
    ("first", "last", "last_loss_year", "years"),
    [
        ("2017-01", "2026-11", 2025, (2017, 2025)),  # GFC stops first
        ("2017-03", "2024-12", 2025, (2018, 2024)),  # 2017 is a part year
        ("2017-01", "2024-06", 2025, (2017, 2023)),  # and so is 2024
    ],
)
def test_only_whole_years_gfc_has_mapped_are_judged(first, last, last_loss_year, years):
    judged = gfc.judged_years(first, last, last_loss_year)
    assert (judged[0], judged[-1]) == years


# Events against GFC's loss


@pytest.mark.parametrize(
    ("loss_year", "matched"), [(2019, 1), (2020, 1), (2021, 1), (2018, 0), (2022, 0)]
)
def test_an_event_matches_loss_in_the_same_or_an_adjacent_year(grid, loss_year, matched):
    pixels = block(grid)
    t = compare(grid, [event(pixels)], staged(grid, [(pixels, loss_year)]))
    assert (t.events, t.matched, t.no_loss) == (1, matched, 1 - matched)
    assert t.matched_share == matched


def test_enough_of_the_footprint_must_be_loss(grid):
    pixels = block(grid, height=20, width=20)  # 400 px; a quarter is 100
    exactly = block(grid, height=10, width=10)
    short = exactly.copy()
    short[40, 40] = False
    assert compare(grid, [event(pixels)], staged(grid, [(exactly, 2020)])).matched == 1
    assert compare(grid, [event(pixels)], staged(grid, [(short, 2020)])).no_loss == 1


def test_an_event_without_trees_in_2000_cannot_be_judged(grid):
    pixels = block(grid)
    t = compare(grid, [event(pixels)], staged(grid, [(pixels, 2020)], treecover=5))
    assert (t.matched, t.no_loss, t.not_forest) == (0, 0, 1)


def test_loss_on_too_little_canopy_counts_for_nothing(grid):
    pixels = block(grid, height=20, width=20)
    cover = np.full((grid.height, grid.width), 80)
    cover[pixels] = CFG.min_treecover - 1
    t = compare(grid, [], staged(grid, [(pixels, 2020)], treecover=cover))
    assert t.patches == 0


def test_an_event_after_gfc_ends_cannot_be_judged(grid):
    pixels = block(grid)
    month = f"{CFG.last_loss_year + 1}-03"
    t = compare(grid, [event(pixels, month)], staged(grid, [(pixels, CFG.last_loss_year)]))
    assert (t.events, t.matched, t.no_loss, t.after_gfc) == (1, 0, 0, 1)


def test_other_categories_are_not_judged(grid):
    pixels = block(grid)
    t = compare(grid, [event(pixels, category="water")], staged(grid, [(pixels, 2020)]))
    assert t.events == 0


# GFC's loss against events


def test_loss_above_the_mapping_unit_that_no_event_covers_is_reported(grid):
    t = compare(grid, [], staged(grid, [(block(grid, height=4, width=4), 2020)]))  # 0.16 ha
    assert (t.patches, t.patches_missed) == (1, 1)


@pytest.mark.parametrize(("height", "width"), [(3, 3), (2, 20)])  # 0.09 ha; a 0.4 ha sliver
def test_loss_events_could_not_show_as_is_not_reported(grid, height, width):
    t = compare(grid, [], staged(grid, [(block(grid, height=height, width=width), 2020)]))
    assert t.patches == 0


def test_a_patch_is_matched_by_vegetation_loss_or_noted_as_another_category(grid):
    pixels = block(grid)
    layers = staged(grid, [(pixels, 2020)])
    assert compare(grid, [event(pixels)], layers).patches_matched == 1
    other = compare(grid, [event(pixels, category="buildings_roads")], layers)
    assert (other.patches_matched, other.patches_other_category) == (0, 1)
    assert other.patches_matched_share == 0


def test_an_event_too_many_years_away_does_not_cover_a_patch(grid):
    pixels = block(grid)
    t = compare(grid, [event(pixels, "2018-06")], staged(grid, [(pixels, 2020)]))
    assert t.patches_missed == 1


def test_loss_outside_the_judged_years_is_left_out(grid):
    t = compare(grid, [], staged(grid, [(block(grid), 2016)]))
    assert t.patches == 0


def test_loss_outside_the_area_is_left_out():
    triangle = make_grid(
        "t", Polygon([(30.674, 50.3845), (30.701, 50.3845), (30.674, 50.396)]), max_km2=10
    )
    corner = block(triangle, y=2, x=triangle.width - 20, height=12, width=12)
    assert not triangle.mask[corner].any()
    assert compare(triangle, [], staged(triangle, [(corner, 2020)])).patches == 0


# What `orbitdiff evaluate` gets


def test_only_events_still_standing_are_compared(grid, tmp_path, monkeypatch):
    pixels = block(grid)
    monkeypatch.setattr(gfc, "read_layers", lambda *_: staged(grid, [(pixels, 2020)]))
    events = [event(pixels, status=s) for s in ("confirmed", "pending", "dismissed", "seasonal")]
    check = gfc.CrossCheck(PIPELINE, tmp_path)

    check.add("production", site(), grid, events, confirmed_only=False)
    check.add("production, confirmed only", site(), grid, events, confirmed_only=True)

    assert check.metrics("production")["gfc_crosscheck_events"] == 2
    assert check.metrics("production, confirmed only")["gfc_crosscheck_events"] == 1


def test_a_dismissed_event_does_not_cover_a_patch(grid, tmp_path, monkeypatch):
    pixels = block(grid)
    monkeypatch.setattr(gfc, "read_layers", lambda *_: staged(grid, [(pixels, 2020)]))
    check = gfc.CrossCheck(PIPELINE, tmp_path)
    check.add("production", site(), grid, [event(pixels, status="dismissed")], False)
    assert check.metrics("production")["gfc_crosscheck_patches_missed"] == 1


def test_output_is_per_area_and_overall_and_says_what_it_is(grid, tmp_path, monkeypatch):
    pixels = block(grid)
    layers = staged(grid, [(pixels, 2020)], bytes_read=1_500_000, full_bytes=400_000_000)
    monkeypatch.setattr(gfc, "read_layers", lambda *_: layers)
    check = gfc.CrossCheck(PIPELINE, tmp_path)
    check.add("production", site("east"), grid, [event(pixels)], False)
    check.add("production", site("west"), grid, [event(pixels)], False)

    table = check.table()
    metrics = check.metrics("production")

    assert "not ground truth" in table
    assert "Source: Hansen/UMD/Google/USGS/NASA" in table
    for area in ("east", "west", "all areas"):
        assert f"| production | {area} |" in table
    assert "Read 1.50 MB of 400 MB for east." in table
    assert all(k.startswith("gfc_crosscheck_") for k in metrics)
    assert metrics["gfc_crosscheck_events"] == 2
    assert metrics["gfc_crosscheck_east_matched"] == 1
    assert metrics["gfc_crosscheck_matched_share"] == 1
    assert metrics["gfc_crosscheck_bytes_read"] == 3_000_000
    assert check.metrics("a method that never ran") == {}
    assert check.params()["gfc_version"] == CFG.version


def test_gfc_being_unreachable_never_costs_the_labelled_evaluation(grid, tmp_path, monkeypatch):
    def offline(*_):
        raise RasterioIOError("offline")

    monkeypatch.setattr(gfc, "read_layers", offline)
    check = gfc.CrossCheck(PIPELINE, tmp_path)
    check.add("production", site(), grid, [event(block(grid))], False)

    assert check.metrics("production") == {}
    assert "GFC could not be read for site." in check.table()


def test_gfc_stays_out_of_detection_alerting_and_promotion():
    imports_gfc = re.compile(r"^\s*(from orbitdiff\.gfc |from orbitdiff import .*\bgfc\b)", re.M)
    users = {
        p.name
        for p in Path(orbitdiff.__file__).parent.glob("*.py")
        if imports_gfc.search(p.read_text())
    }
    assert users == {"cli.py", "evaluation.py"}  # evaluation.py: a type hint, not at runtime
    assert "gfc" in inspect.getsource(cli._evaluate)
    assert "gfc" not in inspect.getsource(cli._promote)
    assert "cross_check" not in inspect.getsource(cli._promote)
