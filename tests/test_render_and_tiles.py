from datetime import date
from urllib.parse import parse_qs, urlsplit

import numpy as np
import pytest
from rasterio.io import MemoryFile
from shapely.geometry import box

from orbitdiff import render, tiles
from orbitdiff.config import REPO_ROOT


def test_titiler_colormap_file_is_the_ramp():
    path = REPO_ROOT / "docker" / "titiler" / "colormaps" / f"{render.NDVI_COLORMAP}.npy"
    # Regenerate with: np.save(path, render.ndvi_lut())
    np.testing.assert_array_equal(np.load(path), render.ndvi_lut())


def test_ndvi_ramp_hits_its_stops_and_clamps():
    rgb = render.ndvi_rgb(np.array([-1.0, -0.3, 0.25, 0.9, 1.0]))
    assert rgb.tolist() == [
        [40, 70, 120],
        [40, 70, 120],
        [226, 200, 112],
        [18, 96, 44],
        [18, 96, 44],
    ]


@pytest.mark.filterwarnings("ignore::rasterio.errors.NotGeoreferencedWarning")  # PNGs
def test_png_round_trips_with_transparency():
    image = render.with_alpha(np.full((3, 4, 3), 200, np.uint8), np.eye(3, 4, dtype=bool))
    with MemoryFile(render.png(image)) as mem, mem.open() as src:
        assert (src.count, src.height, src.width) == (4, 3, 4)
        np.testing.assert_array_equal(src.read(4), np.eye(3, 4) * 255)


def test_event_crops_frame_the_footprint_with_context_and_keep_pixels_square(grid):
    values = np.full((4, grid.height, grid.width), 0.05, np.float32)
    values[:, :, :10] = np.nan  # never observed: transparent, not black
    left, top = grid.transform.c, grid.transform.f
    # A 10 x 10 pixel event on the left edge, in the grid's UTM metres.
    event = box(left, top - 600, left + 100, top - 500)

    image = render.crop(values, grid, event)

    # 24 px (the minimum) around its centre, clipped at the grid edge to 17, scaled x8.
    assert image.shape == (24 * 8, 17 * 8, 4)
    outline = (image == render.OUTLINE).all(axis=-1)
    unobserved = np.zeros(image.shape[:2], bool)
    unobserved[:, : 10 * 8] = True
    assert (image[unobserved & ~outline, 3] == 0).all()
    assert (image[~unobserved & ~outline, 3] == 255).all()
    # The event's rows 50-60 sit at 7-17 in the crop: 56-136 once scaled.
    rows = np.flatnonzero(outline.any(axis=1))
    assert 53 <= rows.min() <= 57 and 135 <= rows.max() <= 139


def test_titiler_urls_name_the_month_and_its_rendering():
    true_color = urlsplit(tiles.tile_url(7, "2021-07", "true_color", 14, 3, 5))
    assert true_color.path == "/cog/tiles/WebMercatorQuad/14/3/5.png"
    q = parse_qs(true_color.query)
    assert q["url"] == ["/data/composites/7/2021-07_display.tif"]
    assert (q["bidx"], q["rescale"]) == (["3", "2", "1"], ["0,3000"])
    ndvi = parse_qs(urlsplit(tiles.tile_url(7, "2021-07", "ndvi", 14, 3, 5)).query)
    assert ndvi["expression"] == ["(b4-b3)/(b4+b3)"] and ndvi["colormap_name"] == ["orbitdiff_ndvi"]
    point = urlsplit(tiles.point_url(7, "2021-07", 30.5, 50.25))
    assert point.path == "/cog/point/30.5,50.25"
    assert parse_qs(point.query)["expression"] == ["(b4-b3)/(b4+b3);b5"]


def test_the_browser_only_gets_versioned_paths_on_the_api():
    paths = tiles.composite_paths(7, date(2021, 7, 1), "abc123")
    assert paths == {
        "true_color": "/aois/7/months/2021-07/tiles/true_color/{z}/{x}/{y}.png?v=abc123",
        "ndvi": "/aois/7/months/2021-07/tiles/ndvi/{z}/{x}/{y}.png?v=abc123",
        "point": "/aois/7/months/2021-07/point/{lon},{lat}?v=abc123",
    }
