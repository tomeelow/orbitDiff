"""Turning reflectance into pictures: true colour, the NDVI ramp, PNG bytes.

One definition of each, shared by the API's scene images, the mask report and the
colormap TiTiler loads for composite tiles, so every view of an AOI looks the same.
"""

import warnings

import numpy as np
from rasterio.errors import NotGeoreferencedWarning
from rasterio.io import MemoryFile

TRUE_COLOR_MAX = 0.3  # reflectance shown as white
# NDVI ramp from the mockup: (value, (r, g, b)), linear between stops, clamped outside.
NDVI_RAMP = (
    (-0.3, (40, 70, 120)),
    (0.0, (150, 105, 70)),
    (0.25, (226, 200, 112)),
    (0.5, (140, 186, 80)),
    (0.9, (18, 96, 44)),
)
NDVI_MIN, NDVI_MAX = NDVI_RAMP[0][0], NDVI_RAMP[-1][0]
# TiTiler loads the ramp from docker/titiler/colormaps/<name>.npy (see tests/test_render.py).
NDVI_COLORMAP = "orbitdiff_ndvi"


def ndvi(red: np.ndarray, nir: np.ndarray) -> np.ndarray:
    with np.errstate(divide="ignore", invalid="ignore"):
        return (nir - red) / (nir + red)


def ndvi_rgb(values: np.ndarray) -> np.ndarray:
    stops = [v for v, _ in NDVI_RAMP]
    channels = [np.interp(values, stops, [c[i] for _, c in NDVI_RAMP]) for i in range(3)]
    return np.stack(channels, axis=-1).round().astype(np.uint8)


def ndvi_lut() -> np.ndarray:
    """The ramp as a 256-entry RGBA table over [NDVI_MIN, NDVI_MAX], TiTiler's colormap format.
    Tile requests rescale NDVI with the same bounds."""
    rgb = ndvi_rgb(np.linspace(NDVI_MIN, NDVI_MAX, 256))
    return np.concatenate([rgb, np.full((256, 1), 255, np.uint8)], axis=1)


def true_color(red: np.ndarray, green: np.ndarray, blue: np.ndarray) -> np.ndarray:
    stack = np.stack([red, green, blue], axis=-1) / TRUE_COLOR_MAX
    return (np.nan_to_num(np.clip(stack, 0, 1)) * 255).astype(np.uint8)


def with_alpha(rgb: np.ndarray, visible: np.ndarray) -> np.ndarray:
    return np.concatenate([rgb, np.where(visible, 255, 0)[..., None].astype(np.uint8)], axis=-1)


def png(image: np.ndarray) -> bytes:
    """Encode (y, x, channels) uint8 as PNG."""
    height, width, count = image.shape
    profile = {"driver": "PNG", "width": width, "height": height, "count": count, "dtype": "uint8"}
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", NotGeoreferencedWarning)  # a picture, not a map
        with MemoryFile() as mem:
            with mem.open(**profile) as dst:
                dst.write(image.transpose(2, 0, 1))
            return mem.read()
