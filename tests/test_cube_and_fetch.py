from collections import Counter
from dataclasses import replace
from datetime import UTC, date, datetime

import numpy as np
import pytest
from rasterio.errors import RasterioIOError

from orbitdiff import catalog, cube, reading
from orbitdiff.config import REPO_ROOT, load_pipeline_config
from orbitdiff.fetch import fetch_aoi
from orbitdiff.reading import read_pass

CFG = load_pipeline_config(REPO_ROOT / "config" / "pipeline.toml")
DROP = CFG.masking.scl_drop


def read(scene, grid):
    return read_pass(
        [scene],
        grid,
        scl_drop=DROP,
        fetch_min_clear_frac=0.05,
        max_tile_cloud_pct=95,
        sign=lambda s, b: s.hrefs[b],
    )


def test_cube_appends_reloads_sorted_and_keeps_provenance(tmp_path, grid, make_scene):
    late = make_scene(
        "late", acquired=datetime(2022, 3, 1, 9, tzinfo=UTC), baseline="04.00", offset_removed=False
    )
    early = make_scene("early", scl_class=9)
    path = cube.cube_path(tmp_path, grid.aoi_id)

    cube.append(path, cube.to_dataset([read(late, grid)], grid))
    cube.append(path, cube.to_dataset([read(early, grid)], grid))
    ds = cube.load(path)

    assert list(ds["item_id"].values) == ["early", "late"]
    assert list(ds["status"].values) == ["scl", "full"]
    assert list(ds["offset_dn"].values) == [0, -1000]
    assert cube.matches_grid(ds, grid)
    assert cube.pass_keys(ds) == {
        ("sentinel-2b", date(2021, 8, 15)),
        ("sentinel-2b", date(2022, 3, 1)),
    }


def test_reflectance_applies_each_pass_offset_and_masks_nodata(tmp_path, grid, make_scene):
    # Same surface, two processing conventions: both must come out at 0.05 reflectance.
    removed = make_scene("removed", band_value=500, baseline="05.00", offset_removed=True)
    kept = make_scene(
        "kept",
        band_value=1500,
        baseline="05.00",
        offset_removed=False,
        acquired=datetime(2021, 9, 1, 9, tzinfo=UTC),
    )
    cloudy = make_scene("cloudy", scl_class=9, acquired=datetime(2021, 9, 6, 9, tzinfo=UTC))
    path = cube.cube_path(tmp_path, grid.aoi_id)
    cube.append(path, cube.to_dataset([read(s, grid) for s in (removed, kept, cloudy)], grid))

    refl = cube.reflectance(cube.load(path))
    np.testing.assert_allclose(refl["red"].isel(time=[0, 1]).values, 0.05, atol=1e-6)
    assert np.isnan(refl["red"].isel(time=2).values).all()  # bands never read: no data


def test_fetch_reads_only_missing_passes(tmp_path, grid, make_scene, monkeypatch):
    scenes = [
        make_scene("a"),
        make_scene("b", acquired=datetime(2021, 8, 18, 9, tzinfo=UTC), scl_class=9),
        make_scene("c", acquired=datetime(2021, 8, 20, 9, tzinfo=UTC), tile_cloud_pct=99),
    ]
    found = list(scenes[:2])
    monkeypatch.setattr(catalog, "search", lambda *a, **k: list(found))
    cfg = CFG.model_copy(update={"reading": CFG.reading.model_copy(update={"workers": 2})})

    first = fetch_aoi(grid, date(2021, 1, 1), date(2021, 12, 31), cfg, tmp_path)
    assert (first.passes, first.cached, first.full, first.scl_only) == (2, 0, 1, 1)

    found.append(scenes[2])
    second = fetch_aoi(grid, date(2021, 1, 1), date(2021, 12, 31), cfg, tmp_path)
    assert (second.passes, second.cached, second.tile_cloud, second.bytes_read) == (3, 2, 1, 0)
    assert cube.load(cube.cube_path(tmp_path, grid.aoi_id)).sizes["time"] == 3


def test_fetch_retries_a_pass_that_failed_and_leaves_one_that_keeps_failing(
    tmp_path, grid, make_scene, monkeypatch
):
    scenes = [make_scene("a"), make_scene("b", acquired=datetime(2021, 8, 18, 9, tzinfo=UTC))]
    monkeypatch.setattr(catalog, "search", lambda *a, **k: list(scenes))
    read_pass, attempts = reading.read_pass, Counter()

    def flaky(candidates, **kwargs):
        name = candidates[0].item_id
        attempts[name] += 1
        if name == "b" or attempts[name] == 1:  # "a" drops once, "b" never answers
            raise RasterioIOError("CURL error: Failed to connect")
        return read_pass(candidates, **kwargs)

    monkeypatch.setattr(reading, "read_pass", flaky)
    reading_cfg = CFG.reading.model_copy(update={"retry_rounds": 2, "retry_wait_s": 0})
    cfg = CFG.model_copy(update={"reading": reading_cfg})
    progress: list[tuple[int, int]] = []

    summary = fetch_aoi(
        grid, date(2021, 1, 1), date(2021, 12, 31), cfg, tmp_path, lambda *p: progress.append(p)
    )

    assert (summary.full, summary.failed) == (1, 1)
    assert attempts == {"a": 2, "b": 3}  # one try and two retry rounds
    assert progress[-1] == (2, 2)  # both settled, one of them as failed
    assert list(cube.load(cube.cube_path(tmp_path, grid.aoi_id))["item_id"].values) == ["a"]


def test_fetch_refuses_a_cube_built_on_another_grid(tmp_path, grid, make_scene, monkeypatch):
    path = cube.cube_path(tmp_path, grid.aoi_id)
    cube.append(path, cube.to_dataset([read(make_scene(), grid)], grid))
    monkeypatch.setattr(catalog, "search", lambda *a, **k: [])
    moved = replace(grid, transform=grid.transform @ grid.transform.translation(1, 0))
    with pytest.raises(ValueError, match="different grid"):
        fetch_aoi(moved, date(2021, 1, 1), date(2021, 12, 31), CFG, tmp_path)
