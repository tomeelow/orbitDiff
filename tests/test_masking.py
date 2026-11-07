from datetime import UTC, datetime

import numpy as np
import pytest
import xarray as xr

from orbitdiff import cube, mask_report, masking
from orbitdiff.config import REPO_ROOT, load_pipeline_config
from orbitdiff.reading import read_pass

CFG = load_pipeline_config(REPO_ROOT / "config" / "pipeline.toml").masking


def dot(shape=(9, 9), at=(4, 4)) -> np.ndarray:
    m = np.zeros(shape, bool)
    m[at] = True
    return m


def test_dilate_grows_a_square_per_time_step():
    stack = np.stack([dot(), np.zeros((9, 9), bool)])
    grown = masking.dilate(stack, 2)
    assert grown[0].sum() == 25 and grown[0, 2:7, 2:7].all()
    assert not grown[1].any()
    np.testing.assert_array_equal(masking.dilate(stack, 0), stack)


def test_scl_clear_grows_clouds_but_not_no_data_and_keeps_snow():
    scl = np.full((9, 9), 4, np.uint8)  # vegetation
    scl[4, 4] = 9  # cloud
    scl[0, :] = 0  # swath edge: no data
    scl[8, 8] = 11  # snow
    clear = masking.scl_clear(scl, CFG.scl_drop, dilate_px=1)
    assert not clear[3:6, 3:6].any()  # the cloud and its 1 px ring
    assert not clear[0].any() and clear[1, 0]  # no data dropped, not grown
    assert clear[8, 8] and masking.snow(scl, CFG.scl_flag)[8, 8]


def test_model_masks_thick_thin_cloud_and_shadow():
    classes = np.array([[0, 1, 2, 3]], np.uint8)
    np.testing.assert_array_equal(
        masking.model_clear(classes, {1, 2, 3}, dilate_px=0), [[True, False, False, False]]
    )


def passes(scl, bands_value=1000, missing_band_at=None) -> xr.Dataset:
    data = {
        b: (("time", "y", "x"), np.full(scl.shape, bands_value, np.uint16))
        for b in ("blue", "green", "red", "nir")
    }
    if missing_band_at:
        data["nir"][1][missing_band_at] = 0
    data["scl"] = (("time", "y", "x"), scl)
    return xr.Dataset(data)


def test_clear_mask_needs_data_in_every_band_and_both_methods_clear():
    scl = np.full((1, 9, 9), 4, np.uint8)
    ds = passes(scl, missing_band_at=(0, 0, 0))
    model = np.zeros_like(scl)
    model[0, 8, 8] = 2  # thin cloud SCL missed
    cfg = CFG.model_copy(update={"dilate_px": 0})

    scl_only = masking.clear_mask(ds, None, cfg)
    both = masking.clear_mask(ds, model, cfg)
    assert not scl_only[0, 0, 0] and scl_only[0, 8, 8]
    assert not both[0, 8, 8] and both.sum() == 9 * 9 - 2


def test_clear_fraction_counts_only_aoi_pixels():
    clear = np.array([[[True, False], [False, False]]])
    aoi = np.array([[True, True], [False, False]])
    np.testing.assert_allclose(masking.clear_fraction(clear, aoi), [0.5])


@pytest.fixture
def fetched(tmp_path, grid, make_scene):
    """A cube with one clear pass, one partly cloudy pass and one SCL-only pass."""

    def read(scene):
        return read_pass(
            [scene],
            grid,
            scl_drop=CFG.scl_drop,
            fetch_min_clear_frac=0.05,
            max_tile_cloud_pct=95,
            sign=lambda s, b: s.hrefs[b],
        )

    scenes = [
        make_scene("clear"),
        make_scene("cloudy", scl_class=9, acquired=datetime(2021, 8, 20, 9, tzinfo=UTC)),
        make_scene("later", acquired=datetime(2021, 8, 25, 9, tzinfo=UTC)),
    ]
    cube.append(
        cube.cube_path(tmp_path, grid.aoi_id), cube.to_dataset([read(s) for s in scenes], grid)
    )
    return tmp_path


def test_update_masks_runs_the_model_once_per_pass_with_bands(fetched, grid):
    seen = []

    def model(red, green, nir):
        seen.append(red.shape)
        return np.zeros(red.shape, np.uint8)

    first = masking.update_masks(grid.aoi_id, CFG, fetched, model)
    again = masking.update_masks(grid.aoi_id, CFG, fetched, model)
    assert (first.passes, first.cached, again.cached) == (2, 0, 2)
    assert seen == [(grid.height, grid.width)] * 2  # the cloudy pass has no bands to show it

    stored = cube.load(masking.mask_path(fetched, grid.aoi_id))
    assert stored.attrs["model_version"] == CFG.cloud_model_version
    assert sorted(stored["item_id"].values) == ["clear", "later"]


def test_update_masks_refuses_a_store_from_another_model_version(fetched, grid):
    masking.update_masks(grid.aoi_id, CFG, fetched, lambda r, g, n: np.zeros(r.shape, np.uint8))
    newer = CFG.model_copy(update={"cloud_model_version": CFG.cloud_model_version + 1})
    with pytest.raises(ValueError, match="deliberate"):
        masking.update_masks(grid.aoi_id, newer, fetched, lambda r, g, n: None)


def test_composites_never_fall_back_to_scl_alone(fetched, grid):
    ds = cube.load(cube.cube_path(fetched, grid.aoi_id))
    masking.update_masks(grid.aoi_id, CFG, fetched, lambda r, g, n: np.ones(r.shape, np.uint8))
    masks = cube.load(masking.mask_path(fetched, grid.aoi_id))

    classes = masking.model_classes_for(ds, masks)
    assert classes[0].all() and not classes[1].any()  # SCL-only pass: nothing to see

    without_last = masks.isel(time=[0])
    with pytest.raises(LookupError, match="later"):
        masking.model_classes_for(ds, without_last)


def test_report_counts_scenes_each_method_lets_through():
    rows = [
        {"item_id": "a", "scl_clear": 0.9, "combined_clear": 0.5},  # model caught haze
        {"item_id": "b", "scl_clear": 0.9, "combined_clear": 0.9},
        {"item_id": "c", "scl_clear": 0.2, "combined_clear": 0.1},
        {"item_id": "d", "scl_clear": 0.9, "combined_clear": 0.9},  # not labelled
    ]
    labels = {"a": False, "b": True, "c": True}
    scl, combined = mask_report.score(rows, labels, min_clear=0.6)
    assert (scl.labelled, scl.let_through, scl.thrown_away) == (3, 1, 1)
    assert (combined.let_through, combined.thrown_away) == (0, 1)


def test_report_writes_contact_sheet_and_label_template(fetched, grid, monkeypatch):
    masking.update_masks(grid.aoi_id, CFG, fetched, lambda r, g, n: np.zeros(r.shape, np.uint8))
    labels = fetched / "labels"
    labels.mkdir()
    (labels / f"{grid.aoi_id}.csv").write_text("item_id,usable\nclear,yes\nlater,no\n")
    monkeypatch.setattr(mask_report, "LABELS_DIR", labels)

    index, scores = mask_report.write_report(grid.aoi_id, CFG, fetched)
    assert (index.parent / "img" / "clear_masks.png").exists()
    template = (index.parent / "passes.csv").read_text().splitlines()
    assert template[0] == "date,item_id,scl_clear,combined_clear,usable"
    assert len(template) == 3  # passes with bands only
    assert [s.let_through for s in scores] == [1, 1]  # "later" is clear to both methods
