from dataclasses import replace

import numpy as np
from helpers import MARGIN_M, tile_for, write_tif
from rasterio.transform import Affine
from rasterio.warp import transform_bounds

from orbitdiff.reading import read_band, read_pass, scl_clear_frac

DROP = {0, 1, 3, 8, 9, 10}


def unsigned(scene, band):
    return scene.hrefs[band]


def test_band_on_its_own_grid_is_read_exactly(tmp_path, grid):
    transform, shape = tile_for(grid, 10)
    data = np.arange(shape[0] * shape[1], dtype=np.uint32).reshape(shape) % 60000 + 1
    href = write_tif(tmp_path / "b.tif", data.astype(np.uint16), grid.epsg, transform)

    r = read_band(href, grid)
    off = MARGIN_M // 10
    np.testing.assert_array_equal(r.data, data[off : off + grid.height, off : off + grid.width])
    assert 0 < r.bytes_read < r.full_bytes


def test_scl_is_upsampled_by_nearest_neighbour(tmp_path, grid):
    transform, shape = tile_for(grid, 20)
    classes = np.random.default_rng(0).integers(1, 12, size=shape, dtype=np.uint8)
    href = write_tif(tmp_path / "scl.tif", classes, grid.epsg, transform)

    r = read_band(href, grid, categorical=True)
    off = MARGIN_M // 20
    expected = classes[off:, off:].repeat(2, axis=0).repeat(2, axis=1)[: grid.height, : grid.width]
    np.testing.assert_array_equal(r.data, expected)


def test_band_from_the_neighbouring_utm_zone_is_reprojected(tmp_path, grid):
    # A constant tile in zone 35 covering the AOI: reprojected, every AOI pixel reads 1234.
    left, _, _, top = transform_bounds(grid.crs, "EPSG:32635", *grid.bounds)
    zone35 = Affine(10, 0, left // 10 * 10 - 1000, 0, -10, top // 10 * 10 + 1000)
    href = write_tif(tmp_path / "z35.tif", np.full((500, 500), 1234, np.uint16), 32635, zone35)
    r = read_band(href, grid)
    assert (r.data[grid.mask] == 1234).all()


def test_clear_fraction_counts_only_aoi_pixels():
    scl = np.array([[4, 9], [8, 4]], np.uint8)
    mask = np.array([[True, True], [False, True]])
    assert scl_clear_frac(scl, mask, DROP) == 2 / 3


def test_cloudy_pass_reads_scl_only(make_scene, grid):
    r = read_pass(
        [make_scene(scl_class=9)],
        grid,
        scl_drop=DROP,
        fetch_min_clear_frac=0.05,
        max_tile_cloud_pct=95,
        sign=unsigned,
    )
    assert (r.status, r.bands, r.clear_frac) == ("scl", None, 0.0)
    assert r.bytes_read > 0


def test_clear_pass_reads_all_bands(make_scene, grid):
    r = read_pass(
        [make_scene(scl_class=4)],
        grid,
        scl_drop=DROP,
        fetch_min_clear_frac=0.05,
        max_tile_cloud_pct=95,
        sign=unsigned,
    )
    assert r.status == "full" and r.clear_frac == 1.0
    assert set(r.bands) == {"blue", "green", "red", "nir"}
    assert (r.bands["red"] == 1500).all()


def test_tile_cloud_prefilter_reads_nothing(make_scene, grid):
    r = read_pass(
        [make_scene(tile_cloud_pct=99)],
        grid,
        scl_drop=DROP,
        fetch_min_clear_frac=0.05,
        max_tile_cloud_pct=95,
        sign=unsigned,
    )
    assert (r.status, r.scl, r.bytes_read) == ("tile_cloud", None, 0)


def test_failed_candidate_falls_back_to_the_next(make_scene, grid, tmp_path):
    good = make_scene()
    broken = replace(
        good, item_id="broken", hrefs={**good.hrefs, "scl": str(tmp_path / "missing.tif")}
    )
    r = read_pass(
        [broken, good],
        grid,
        scl_drop=DROP,
        fetch_min_clear_frac=0.05,
        max_tile_cloud_pct=95,
        sign=unsigned,
    )
    assert r.scene.item_id == good.item_id


def test_every_candidate_failing_returns_none(make_scene, grid, tmp_path):
    s = make_scene()
    broken = replace(s, hrefs={**s.hrefs, "scl": str(tmp_path / "missing.tif")})
    assert (
        read_pass(
            [broken],
            grid,
            scl_drop=DROP,
            fetch_min_clear_frac=0.05,
            max_tile_cloud_pct=95,
            sign=unsigned,
        )
        is None
    )
