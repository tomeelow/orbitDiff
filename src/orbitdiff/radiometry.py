"""Digital numbers to surface reflectance, the same way for every catalog (ADR-003).

Processing baseline 04.00 (25 Jan 2022) added a -1000 offset to L2A digital numbers.
Earth Search removes it from some items and says so in `earthsearch:boa_offset_applied`,
while still publishing a `raster:bands` offset of -0.1 on those items. That metadata is
ignored here: following it would subtract the offset twice (docs/spikes.md, section 2).
"""

import re

import numpy as np

OFFSET_BASELINE = "04.00"
BOA_OFFSET_DN = -1000
QUANTIFICATION = 10_000
NODATA_DN = 0
_BASELINE = re.compile(r"^\d{2}\.\d{2}$")


def offset_dn(processing_baseline: str, offset_removed: bool) -> int:
    """The DN offset to add before scaling, for one item."""
    if not _BASELINE.match(processing_baseline):
        raise ValueError(f"unexpected processing baseline {processing_baseline!r}")
    return BOA_OFFSET_DN if processing_baseline >= OFFSET_BASELINE and not offset_removed else 0


def to_reflectance(dn: np.ndarray, offset: int | np.ndarray) -> np.ndarray:
    """Reflectance as float32, with NaN where the DN is nodata."""
    refl = (dn.astype(np.float32) + offset) / QUANTIFICATION
    return np.where(dn == NODATA_DN, np.float32(np.nan), refl)


def dark_target(
    blue: np.ndarray, red: np.ndarray, reference: np.ndarray, percentile: float, min_pixels: int
) -> np.ndarray:
    """Per pass (time, y, x) -> (time,): a low percentile of min(blue, red) reflectance
    over the reference pixels; NaN with fewer than `min_pixels` of them.

    Vegetation is dark in red and water in blue, so a correctly harmonized pass reads a
    little above zero here in any season. A pass whose offset was subtracted although its
    pixels didn't carry it reads 0.1 lower, below zero (docs/spikes.md, section 5).
    """
    darker = np.minimum(blue, red)
    out = np.full(darker.shape[0], np.nan, np.float32)
    for t, (values, ref) in enumerate(zip(darker, reference, strict=True)):
        values = values[ref & ~np.isnan(values)]
        if values.size >= min_pixels:
            out[t] = np.percentile(values, percentile)
    return out
