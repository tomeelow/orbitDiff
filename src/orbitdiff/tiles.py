"""Composite map tiles: TiTiler renders them (ADR-007), the API hands them out (ADR-013).

TiTiler reads whatever file its `url` parameter names, so browsers never reach it. The web
app asks the API, which checks who owns the area, builds TiTiler's URL here, and relays the
answer. Every URL the browser sees carries the month's version, so its tiles never change.
"""

import urllib.error
import urllib.request
from datetime import date
from typing import Literal
from urllib.parse import urlencode

from orbitdiff import render
from orbitdiff.composites import DISPLAY_BANDS, SCALE
from orbitdiff.config import settings

Layer = Literal["true_color", "ndvi"]
TIMEOUT_S = 30


def _band(name: str) -> str:
    return f"b{DISPLAY_BANDS.index(name) + 1}"


def _ndvi() -> str:
    red, nir = _band("red"), _band("nir")
    return f"({nir}-{red})/({nir}+{red})"


def _upstream(path: str, aoi_id: int, month: str, **params: object) -> str:
    s = settings()
    cog = f"{s.titiler_data_dir}/composites/{aoi_id}/{month}_display.tif"
    return f"{s.titiler_url}{path}?" + urlencode({"url": cog, **params}, doseq=True)


def tile_url(aoi_id: int, month: str, layer: Layer, z: int, x: int, y: int) -> str:
    """TiTiler's URL for one tile of a month's display composite."""
    path = f"/cog/tiles/WebMercatorQuad/{z}/{x}/{y}.png"
    if layer == "true_color":
        return _upstream(
            path,
            aoi_id,
            month,
            bidx=[DISPLAY_BANDS.index(b) + 1 for b in ("red", "green", "blue")],
            rescale=f"0,{round(render.TRUE_COLOR_MAX * SCALE)}",
            tilesize=512,
        )
    return _upstream(
        path,
        aoi_id,
        month,
        expression=_ndvi(),
        rescale=f"{render.NDVI_MIN},{render.NDVI_MAX}",
        colormap_name=render.NDVI_COLORMAP,
        tilesize=512,
    )


def point_url(aoi_id: int, month: str, lon: float, lat: float) -> str:
    """TiTiler's URL for the hover readout: values are [NDVI, 1 if the pixel was filled for
    display else 0]. Overriding nodata (0) keeps unfilled pixels (filled = 0) from masking
    the NDVI; 65535 is reflectance 6.5, which no surface reaches."""
    return _upstream(
        f"/cog/point/{lon},{lat}",
        aoi_id,
        month,
        expression=f"{_ndvi()};{_band('filled')}",
        nodata=65535,
    )


def composite_paths(aoi_id: int, month: date, version: str) -> dict[str, str]:
    """Where the web app gets a month's tiles: paths on the API, with {z}/{x}/{y} and
    {lon},{lat} left for it to fill in."""
    base = f"/aois/{aoi_id}/months/{month:%Y-%m}"
    return {
        "true_color": f"{base}/tiles/true_color/{{z}}/{{x}}/{{y}}.png?v={version}",
        "ndvi": f"{base}/tiles/ndvi/{{z}}/{{x}}/{{y}}.png?v={version}",
        "point": f"{base}/point/{{lon}},{{lat}}?v={version}",
    }


def fetch(url: str) -> tuple[int, bytes, str]:
    """TiTiler's answer: status, body and media type. Raises only when it can't be reached."""
    try:
        with urllib.request.urlopen(url, timeout=TIMEOUT_S) as r:
            return r.status, r.read(), r.headers.get_content_type()
    except urllib.error.HTTPError as error:
        return error.code, b"", ""
