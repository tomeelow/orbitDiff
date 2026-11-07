import json
import math
from dataclasses import replace
from datetime import UTC, date, datetime

import numpy as np
import pytest
import rasterio
import xarray as xr

from orbitdiff import composites, cube, masking
from orbitdiff.config import BANDS, REPO_ROOT, load_pipeline_config
from orbitdiff.reading import PassRead

CFG = load_pipeline_config(REPO_ROOT / "config" / "pipeline.toml")
nan = np.nan


def test_median_uses_only_clear_observations():
    refl = xr.Dataset({b: (("time", "y", "x"), [[[0.1]], [[0.5]], [[0.2]]]) for b in BANDS})
    clear = np.array([[[True]], [[False]], [[True]]])
    snow = np.array([[[True]], [[True]], [[False]]])
    comp = composites.median_composite(refl, clear, snow)
    assert comp["red"][0, 0] == pytest.approx(0.15)  # the cloudy 0.5 never counts
    assert (comp["n_clear"][0, 0], comp["n_snow"][0, 0]) == (2, 1)


def test_pixel_without_a_clear_observation_stays_empty():
    refl = xr.Dataset({b: (("time", "y", "x"), [[[0.1, 0.1]]]) for b in BANDS})
    comp = composites.median_composite(refl, np.array([[[True, False]]]), np.zeros((1, 1, 2), bool))
    assert not math.isnan(comp["nir"][0, 0]) and math.isnan(comp["nir"][0, 1])
    assert comp["n_clear"].tolist() == [[1, 0]]


def month(*values) -> np.ndarray:
    """A (bands=1, y=1, x=n) composite."""
    return np.array([[values]], np.float32)


def test_gap_fill_averages_the_nearest_clear_month_on_each_side():
    target = month(0.3, nan, nan, nan)
    before = [month(nan, 0.1, nan, nan), month(nan, 0.9, 0.2, nan)]  # nearest first
    after = [month(nan, 0.3, nan, nan)]
    filled, was_filled = composites.gap_fill(target, before, after)
    np.testing.assert_allclose(filled[0, 0], [0.3, 0.2, 0.2, nan])
    assert was_filled[0].tolist() == [False, True, True, False]  # observed pixels untouched


def test_gap_fill_with_no_neighbours_changes_nothing():
    target = month(nan, 0.2)
    filled, was_filled = composites.gap_fill(target, [], [])
    np.testing.assert_array_equal(filled, target)
    assert not was_filled.any()


def test_months_cover_the_whole_range_including_empty_months():
    first, last = np.datetime64("2020-11-20T09:00"), np.datetime64("2021-02-03T09:00")
    assert composites.months_between(first, last) == ["2020-11", "2020-12", "2021-01", "2021-02"]


def test_reflectance_quantization_never_collides_with_no_data():
    q = composites._to_uint16(np.array([nan, 0.0, 0.00004, 0.1234, 7.0]))
    assert q.tolist() == [0, 1, 1, 1234, 65535]


# A small archive: January clear, February without passes, March cloudy over the left
# half every time, April with a pass skipped on tile cloud. Arrival order is shuffled.
PASSES = [
    (date(2021, 1, 5), "full", None),
    (date(2021, 1, 20), "full", None),
    (date(2021, 3, 2), "full", "left"),
    (date(2021, 3, 12), "scl", None),
    (date(2021, 4, 1), "full", None),
    (date(2021, 3, 22), "full", "left"),
    (date(2021, 4, 16), "tile_cloud", None),
]


def pass_read(base, grid, day: date, status: str, cloud: str | None) -> PassRead:
    scene = replace(
        base,
        item_id=f"S2B_36UUA_{day:%Y%m%d}_0_L2A",
        acquired=datetime(day.year, day.month, day.day, 9, 1, tzinfo=UTC),
    )
    if status == "tile_cloud":
        return PassRead(scene, status, None, None, nan, 0, 0)
    shape = (grid.height, grid.width)
    scl = np.full(shape, 4, np.uint8)
    if cloud == "left":
        scl[:, : grid.width // 2] = 9
    rng = np.random.default_rng(day.toordinal())
    bands = {b: rng.integers(500, 3000, shape, dtype=np.uint16) for b in BANDS}
    return PassRead(scene, status, scl, None if status == "scl" else bands, 0.5, 1, 10)


def bright_red_is_cloud(red, green, nir) -> np.ndarray:
    return (red > 0.2995).astype(np.uint8)  # a stand-in model: rare bright specks


def run(data_dir, grid, reads) -> None:
    cube.append(cube.cube_path(data_dir, grid.aoi_id), cube.to_dataset(reads, grid))
    masking.update_masks(grid.aoi_id, CFG.masking, data_dir, bright_red_is_cloud)
    composites.update_composites(grid.aoi_id, CFG, data_dir)


def read_all(data_dir, aoi_id) -> dict[str, np.ndarray]:
    out = {}
    for path in sorted(composites.composite_dir(data_dir, aoi_id).glob("*.tif")):
        with rasterio.open(path) as src:
            out[path.name] = src.read()
    return out


@pytest.fixture
def archive(grid, make_scene):
    base = make_scene()
    return [pass_read(base, grid, *p) for p in PASSES]


def test_backfill_and_pass_by_pass_updates_give_identical_composites(tmp_path, grid, archive):
    run(tmp_path / "backfill", grid, archive)
    for read in archive:
        run(tmp_path / "incremental", grid, [read])

    backfilled = read_all(tmp_path / "backfill", grid.aoi_id)
    incremental = read_all(tmp_path / "incremental", grid.aoi_id)
    assert sorted(backfilled) == sorted(incremental)
    assert len(backfilled) == 8  # 4 months x (detection + display)
    for name, data in backfilled.items():
        np.testing.assert_array_equal(data, incremental[name], err_msg=name)
    index = "months.json"
    a, b = (
        json.loads((composites.composite_dir(tmp_path / d, grid.aoi_id) / index).read_text())
        for d in ("backfill", "incremental")
    )
    assert a == b


def test_month_stats_and_display_fill(tmp_path, grid, archive):
    run(tmp_path, grid, archive)
    months = {m.month: m for m in composites.read_months(tmp_path, grid.aoi_id)}

    assert (months["2021-02"].passes, months["2021-02"].observed_frac) == (0, 0.0)
    assert months["2021-02"].filled_frac > 0.95  # from January and March
    march = months["2021-03"]
    assert (march.passes, march.with_bands, march.clear_scenes) == (3, 2, 0)
    assert 0.3 < march.observed_frac < 0.7 and march.filled_frac > 0.3
    assert months["2021-04"].passes == 2

    out = composites.composite_dir(tmp_path, grid.aoi_id)
    detection = composites.read_composite(out / "2021-03.tif")
    assert np.isnan(detection[:, :, : grid.width // 2]).all()  # never filled for detection
    with rasterio.open(out / "2021-03_display.tif") as src:
        assert src.descriptions == ("blue", "green", "red", "nir", "filled")
        filled = src.read(5).astype(bool)
    # Left half filled; right half observed, bar specks the model masked in both passes.
    assert filled[:, : grid.width // 2].mean() > 0.95
    assert filled[:, grid.width // 2 + 3 :].mean() < 0.01


def test_changed_mask_settings_recompute_every_month(tmp_path, grid, archive):
    run(tmp_path, grid, archive)
    wider = CFG.model_copy(update={"masking": CFG.masking.model_copy(update={"dilate_px": 3})})
    again = composites.update_composites(grid.aoi_id, wider, tmp_path)
    assert (again.computed, again.displayed) == (4, 4)
    assert composites.update_composites(grid.aoi_id, wider, tmp_path).computed == 0


def test_composites_refuse_passes_the_cloud_model_has_not_seen(tmp_path, grid, archive):
    cube.append(cube.cube_path(tmp_path, grid.aoi_id), cube.to_dataset(archive, grid))
    with pytest.raises(LookupError, match="update_masks"):
        composites.update_composites(grid.aoi_id, CFG, tmp_path)
