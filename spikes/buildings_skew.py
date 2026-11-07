"""Spike 6: how far the L2A the building model serves on sits from the L1C it trained on.

OSCD is top-of-atmosphere (L1C); our composites are surface reflectance (L2A). Three
comparisons, OSCD's test cities against our own areas' anniversary pairs (a month and the
same month a year earlier):

1. raw reflectance, per band: the gap the model would see without standardising;
2. the spread of the standardised after-minus-before difference, per band: what the
   model actually sees;
3. how often the model flags a pixel: on OSCD, unchanged pixels (its false-positive rate)
   and changed ones (its recall); on our pairs, per year, all pixels.

Run after `orbitdiff train-buildings`:

    uv run --group ml python spikes/buildings_skew.py --version 1
"""

import argparse
from collections import defaultdict

import mlflow
import numpy as np

from orbitdiff import detection, oscd, siamese
from orbitdiff.config import BANDS, pipeline_config, settings

parser = argparse.ArgumentParser()
parser.add_argument("--version", type=int, required=True, help="registered model version")
args = parser.parse_args()

cfg, data_dir = pipeline_config().buildings, settings().data_dir
mlflow.set_tracking_uri(settings().mlflow_tracking_uri)
model = siamese.from_registry(cfg.registered_model, args.version)
folder = data_dir / "oscd"


def l2a_pairs():
    for aoi in sorted(p.name for p in (data_dir / "composites").iterdir()):
        stack = detection.load_stack(data_dir, aoi)
        for month in stack.months:
            reference = stack.at(detection.shift_month(month, -detection.YEAR))
            if reference is not None:
                yield aoi, month, reference, stack.at(month)


def diff_spread(before, after) -> np.ndarray:
    """NaN where too little is seen on both dates to standardise, as the model refuses."""
    b, a, seen = siamese.standardize(before, after)
    if seen.sum() < siamese.MIN_SEEN_PX:
        return np.full(len(BANDS), np.nan)
    return (a - b)[:, seen].std(axis=1)


oscd_pairs = [oscd.load_city(folder, n, "test") for n in oscd.cities(folder, "test")]
ours = list(l2a_pairs())
print(
    f"OSCD test: {len(oscd_pairs)} pairs. Ours: {len(ours)} anniversary pairs, areas "
    f"{sorted({aoi for aoi, *_ in ours})}"
)

print("\n1. Median reflectance per band (all pixels, both dates)")
oscd_level = np.median(
    np.concatenate(
        [np.concatenate([c.before, c.after], axis=1).reshape(4, -1) for c in oscd_pairs], axis=1
    ),
    axis=1,
)
ours_all = np.concatenate(
    [np.concatenate([b, a], axis=1).reshape(4, -1) for *_, b, a in ours], axis=1
)
ours_level = np.nanmedian(ours_all, axis=1)
for band, x, y in zip(BANDS, oscd_level, ours_level, strict=True):
    print(f"   {band:6} OSCD L1C {x:.3f}   ours L2A {y:.3f}   ratio {x / y:.2f}")

print("\n2. Spread of the standardised difference, after - before (median over pairs)")
oscd_spread = np.median([diff_spread(c.before, c.after) for c in oscd_pairs], axis=0)
ours_spread = np.nanmedian([diff_spread(b, a) for *_, b, a in ours], axis=0)
for band, x, y in zip(BANDS, oscd_spread, ours_spread, strict=True):
    print(f"   {band:6} OSCD {x:.2f}   ours {y:.2f}")

print(f"\n3. Pixels flagged at threshold {cfg.threshold}")
fp = tp = unchanged = changed = 0
for c in oscd_pairs:
    found = model(c.before, c.after) > cfg.threshold
    fp, unchanged = fp + (found & ~c.changed).sum(), unchanged + (~c.changed).sum()
    tp, changed = tp + (found & c.changed).sum(), changed + c.changed.sum()
print(
    f"   OSCD test: {fp / unchanged:.2%} of unchanged pixels (false positives), "
    f"{tp / changed:.1%} of changed ones"
)
by_year = defaultdict(lambda: [0, 0])
for aoi, month, before, after in ours:
    p = model(before, after)
    seen = ~np.isnan(p)
    by_year[(aoi, month[:4])][0] += int((p[seen] > cfg.threshold).sum())
    by_year[(aoi, month[:4])][1] += int(seen.sum())
for (aoi, year), (flagged, seen) in sorted(by_year.items()):
    print(f"   area {aoi} {year}: {flagged / seen:.2%} of pixels seen on both dates")
