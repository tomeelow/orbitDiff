import json

import pytest
from shapely.geometry import Point, box

from orbitdiff.aoi import RESOLUTION_M, load_geometry, make_grid, utm_epsg


@pytest.mark.parametrize(
    ("lon", "lat", "epsg"),
    [(30.69, 50.39, 32636), (29.99, 50.39, 32635), (-58.4, -34.6, 32721), (179.9, 10, 32660)],
)
def test_utm_zone(lon, lat, epsg):
    assert utm_epsg(lon, lat) == epsg


def test_grid_is_snapped_to_the_sentinel2_pixel_grid(grid):
    left, bottom, right, top = grid.bounds
    assert all(v % RESOLUTION_M == 0 for v in (left, bottom, right, top))
    assert grid.epsg == 32636
    assert (grid.width, grid.height) == ((right - left) / 10, (top - bottom) / 10)


def test_mask_covers_the_polygon(grid):
    # Pixel count times 100 m² matches the polygon's area to within its edge pixels.
    assert grid.mask.sum() * 100 / 1e6 == pytest.approx(grid.area_km2, rel=0.03)
    assert grid.mask.any() and not grid.mask.all()  # a lon/lat box is skewed in UTM


def test_size_cap_is_enforced():
    with pytest.raises(ValueError, match="cap"):
        make_grid("big", box(30.0, 50.0, 30.2, 50.2), max_km2=10)


@pytest.mark.parametrize("geom", [Point(30.7, 50.39), box(30.7, 50.39, 30.7, 50.39)])
def test_not_a_polygon_is_rejected(geom):
    with pytest.raises(ValueError):
        make_grid("bad", geom, max_km2=10)


@pytest.mark.parametrize("wrap", ["geometry", "feature", "collection"])
def test_load_geometry(tmp_path, wrap, aoi_polygon):
    geom = aoi_polygon.__geo_interface__
    doc = {
        "geometry": geom,
        "feature": {"type": "Feature", "properties": {}, "geometry": geom},
        "collection": {
            "type": "FeatureCollection",
            "features": [{"type": "Feature", "properties": {}, "geometry": geom}],
        },
    }[wrap]
    path = tmp_path / "aoi.geojson"
    path.write_text(json.dumps(doc))
    assert make_grid("x", load_geometry(path), 10).area_km2 > 0
