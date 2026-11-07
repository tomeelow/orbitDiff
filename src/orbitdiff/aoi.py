"""An area of interest and the fixed 10 m grid every read for it lands on.

Geometries are stored in EPSG:4326. All pixel work happens in the AOI's UTM zone, on a
grid snapped to multiples of 10 m so it lines up with Sentinel-2's own pixel grid. The
grid never changes for an AOI, so month A and month B overlay pixel for pixel.
"""

import json
import math
from dataclasses import dataclass
from functools import cached_property
from pathlib import Path

import numpy as np
import shapely
from pyproj import CRS, Transformer
from rasterio.features import geometry_mask
from rasterio.transform import Affine
from shapely.geometry import Polygon, shape
from shapely.geometry.base import BaseGeometry

RESOLUTION_M = 10


def reproject(geom: BaseGeometry, src_epsg: int, dst_epsg: int) -> BaseGeometry:
    t = Transformer.from_crs(src_epsg, dst_epsg, always_xy=True)
    return shapely.transform(geom, lambda xy: np.column_stack(t.transform(xy[:, 0], xy[:, 1])))


def utm_epsg(lon: float, lat: float) -> int:
    """Standard UTM zone for a point (ignores the Norway/Svalbard exceptions)."""
    zone = min(int((lon + 180) // 6) + 1, 60)
    return (32600 if lat >= 0 else 32700) + zone


@dataclass(frozen=True)
class AOIGrid:
    aoi_id: str
    geometry: BaseGeometry  # EPSG:4326
    epsg: int
    transform: Affine
    width: int
    height: int

    @property
    def crs(self) -> CRS:
        return CRS.from_epsg(self.epsg)

    @property
    def bounds(self) -> tuple[float, float, float, float]:
        """(left, bottom, right, top) in the AOI's UTM CRS."""
        left, top = self.transform.c, self.transform.f
        return left, top - self.height * RESOLUTION_M, left + self.width * RESOLUTION_M, top

    @cached_property
    def geometry_utm(self) -> BaseGeometry:
        return reproject(self.geometry, 4326, self.epsg)

    @cached_property
    def mask(self) -> np.ndarray:
        """True for pixels whose centre lies inside the AOI polygon."""
        return geometry_mask(
            [self.geometry_utm],
            out_shape=(self.height, self.width),
            transform=self.transform,
            invert=True,
        )

    @property
    def area_km2(self) -> float:
        return self.geometry_utm.area / 1e6


def load_geometry(path: Path) -> dict:
    """The polygon from a GeoJSON file: a geometry, a Feature, or a one-feature collection."""
    doc = json.loads(path.read_text())
    if doc.get("type") == "FeatureCollection":
        if len(doc["features"]) != 1:
            raise ValueError("expected exactly one feature")
        doc = doc["features"][0]
    return doc["geometry"] if doc.get("type") == "Feature" else doc


def make_grid(aoi_id: str, geometry: BaseGeometry | dict, max_km2: float) -> AOIGrid:
    geom = shape(geometry) if isinstance(geometry, dict) else geometry
    if not isinstance(geom, Polygon) or geom.is_empty or not geom.is_valid:
        raise ValueError("an AOI must be a single valid polygon")

    centroid = geom.centroid
    epsg = utm_epsg(centroid.x, centroid.y)
    minx, miny, maxx, maxy = reproject(geom, 4326, epsg).bounds

    left = math.floor(minx / RESOLUTION_M) * RESOLUTION_M
    bottom = math.floor(miny / RESOLUTION_M) * RESOLUTION_M
    right = math.ceil(maxx / RESOLUTION_M) * RESOLUTION_M
    top = math.ceil(maxy / RESOLUTION_M) * RESOLUTION_M
    grid = AOIGrid(
        aoi_id=aoi_id,
        geometry=geom,
        epsg=epsg,
        transform=Affine(RESOLUTION_M, 0, left, 0, -RESOLUTION_M, top),
        width=(right - left) // RESOLUTION_M,
        height=(top - bottom) // RESOLUTION_M,
    )
    if grid.area_km2 > max_km2:
        raise ValueError(f"AOI is {grid.area_km2:.1f} km²; the cap is {max_km2} km²")
    return grid
