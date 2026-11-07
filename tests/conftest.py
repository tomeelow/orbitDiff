"""Shared fixtures. Tests run offline: Python sockets reach only localhost (the test
database), and every raster a test reads is a small tiled GeoTIFF written to a temp dir,
standing in for a COG."""

import socket
from datetime import UTC, datetime

import numpy as np
import pytest
from helpers import tile_for, write_tif
from shapely.geometry import box

from orbitdiff.aoi import AOIGrid, make_grid
from orbitdiff.catalog import SceneItem

# A ~1.9 x 1.3 km area near Kyiv, as in the mockup. UTM zone 36.
AOI = box(30.674, 50.3845, 30.701, 50.396)


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    """Only local services (the test database) are reachable."""
    connect = socket.socket.connect

    def local_only(sock, address):
        host = address[0] if isinstance(address, tuple) else "unix"
        if host not in ("127.0.0.1", "::1", "localhost", "unix"):
            raise RuntimeError(f"tests must not touch the network ({host})")
        return connect(sock, address)

    monkeypatch.setattr(socket.socket, "connect", local_only)


@pytest.fixture
def aoi_polygon():
    return AOI


@pytest.fixture
def grid() -> AOIGrid:
    return make_grid("kyiv", AOI, max_km2=10)


@pytest.fixture
def make_scene(tmp_path, grid):
    """A SceneItem whose assets are local files: constant-valued 10 m bands and a 20 m SCL."""

    def make(
        name: str = "S2B_36UUA_20210815_1_L2A",
        *,
        scl_class: int = 4,
        band_value: int = 1500,
        acquired: datetime = datetime(2021, 8, 15, 8, 58, 45, tzinfo=UTC),
        platform: str = "sentinel-2b",
        baseline: str = "05.00",
        offset_removed: bool = True,
        tile_cloud_pct: float = 10.0,
        catalog: str = "earth-search",
    ) -> SceneItem:
        folder = tmp_path / name
        folder.mkdir(exist_ok=True)
        t10, shape10 = tile_for(grid, 10)
        t20, shape20 = tile_for(grid, 20)
        hrefs = {
            band: write_tif(
                folder / f"{band}.tif", np.full(shape10, band_value, np.uint16), grid.epsg, t10
            )
            for band in ("blue", "green", "red", "nir")
        }
        hrefs["scl"] = write_tif(
            folder / "scl.tif", np.full(shape20, scl_class, np.uint8), grid.epsg, t20
        )
        return SceneItem(
            item_id=name,
            catalog=catalog,
            acquired=acquired,
            platform=platform,
            tile="36UUA",
            epsg=grid.epsg,
            baseline=baseline,
            offset_removed=offset_removed,
            tile_cloud_pct=tile_cloud_pct,
            footprint=AOI.buffer(0.5),
            hrefs=hrefs,
        )

    return make
