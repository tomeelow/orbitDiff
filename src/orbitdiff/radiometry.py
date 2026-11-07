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
