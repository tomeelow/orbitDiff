"""Tile and image URLs the web app uses.

Composite tiles come from TiTiler (ADR-007), reading the display COGs. Every URL carries
the month's version, so a URL's tiles never change and TiTiler marks them immutable.
"""

from datetime import date
from urllib.parse import urlencode

from orbitdiff import render
from orbitdiff.composites import DISPLAY_BANDS, SCALE
from orbitdiff.config import settings

XYZ = "/cog/tiles/WebMercatorQuad/{z}/{x}/{y}.png"


def _band(name: str) -> str:
    return f"b{DISPLAY_BANDS.index(name) + 1}"


def composite_urls(aoi_id: int, month: date, version: str) -> dict[str, str]:
    s = settings()
    cog = f"{s.titiler_data_dir}/composites/{aoi_id}/{month:%Y-%m}_display.tif"
    red, nir = _band("red"), _band("nir")
    ndvi = f"({nir}-{red})/({nir}+{red})"

    def url(path: str, **params: object) -> str:
        return f"{s.titiler_url}{path}?" + urlencode(
            {"url": cog, **params, "v": version}, doseq=True
        )

    return {
        "true_color": url(
            XYZ,
            bidx=[DISPLAY_BANDS.index(b) + 1 for b in ("red", "green", "blue")],
            rescale=f"0,{round(render.TRUE_COLOR_MAX * SCALE)}",
            tilesize=512,
        ),
        "ndvi": url(
            XYZ,
            expression=ndvi,
            rescale=f"{render.NDVI_MIN},{render.NDVI_MAX}",
            colormap_name=render.NDVI_COLORMAP,
            tilesize=512,
        ),
        # Hover readout: values are [NDVI, 1 if the pixel was filled for display else 0].
        # Overriding nodata (0) keeps unfilled pixels (filled = 0) from masking the NDVI;
        # 65535 is reflectance 6.5, which no surface reaches.
        "point": url(
            "/cog/point/{lon},{lat}", expression=f"{ndvi};{_band('filled')}", nodata=65535
        ),
    }
