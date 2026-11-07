"""SCL alone vs SCL + cloud model, for a person to judge (Phase 2's "how to know it works").

There are no cloud labels to score against until someone looks. So the report:

1. writes a contact sheet: every pass with bands, true colour beside an overlay of what
   each method masks (blue: SCL; magenta: only the model);
2. writes `passes.csv` with both methods' AOI clear fractions and an empty `usable` column;
3. once labelled rows (yes/no) are copied to `eval/scene_usability/<aoi>.csv`, counts the
   unusable scenes each method lets through and the usable ones it throws away.
"""

import csv
import html
import warnings
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import rasterio
from rasterio.errors import NotGeoreferencedWarning

from orbitdiff import cube, masking
from orbitdiff.config import REPO_ROOT, MaskingConfig

LABELS_DIR = REPO_ROOT / "eval" / "scene_usability"
TRUE_COLOR_MAX = 0.3  # reflectance mapped to white
SCL_TINT = np.array([40, 110, 255])
MODEL_TINT = np.array([255, 40, 200])


@dataclass(frozen=True)
class MethodScore:
    method: str
    labelled: int
    let_through: int  # labelled unusable, method calls it clear enough
    thrown_away: int  # labelled usable, method calls it too cloudy


def _rgb(red: np.ndarray, green: np.ndarray, blue: np.ndarray) -> np.ndarray:
    stack = np.stack([red, green, blue], axis=-1) / TRUE_COLOR_MAX
    return (np.nan_to_num(np.clip(stack, 0, 1)) * 255).astype(np.uint8)


def _tint(rgb: np.ndarray, where: np.ndarray, color: np.ndarray) -> np.ndarray:
    out = rgb.astype(np.float32)
    out[where] = 0.4 * out[where] + 0.6 * color
    return out.astype(np.uint8)


def _write_png(path: Path, rgb: np.ndarray) -> None:
    height, width, _ = rgb.shape
    profile = {"driver": "PNG", "width": width, "height": height, "count": 3, "dtype": "uint8"}
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", NotGeoreferencedWarning)  # a picture, not a map
        with rasterio.open(path, "w", **profile) as dst:
            dst.write(rgb.transpose(2, 0, 1))


def score(rows: list[dict], labels: dict[str, bool], min_clear: float) -> list[MethodScore]:
    scores = []
    for method in ("scl_clear", "combined_clear"):
        labelled = [r for r in rows if r["item_id"] in labels]
        calls_clear = {r["item_id"]: r[method] >= min_clear for r in labelled}
        scores.append(
            MethodScore(
                method="SCL alone" if method == "scl_clear" else "SCL + model",
                labelled=len(labelled),
                let_through=sum(calls_clear[i] and not labels[i] for i in calls_clear),
                thrown_away=sum(not calls_clear[i] and labels[i] for i in calls_clear),
            )
        )
    return scores


def read_labels(path: Path) -> dict[str, bool]:
    if not path.exists():
        return {}
    with path.open() as f:
        return {
            r["item_id"]: r["usable"].strip().lower() == "yes"
            for r in csv.DictReader(f)
            if r["usable"].strip()
        }


def write_report(aoi_id: str, cfg: MaskingConfig, data_dir: Path) -> tuple[Path, list[MethodScore]]:
    ds = cube.load(cube.cube_path(data_dir, aoi_id))
    masks = cube.load(masking.mask_path(data_dir, aoi_id))
    if ds is None or masks is None:
        raise FileNotFoundError(f"{aoi_id} needs a cube and model masks first")
    grid = cube.grid_of(ds)
    full = ds.isel(time=np.flatnonzero(ds["status"].values == "full"))
    classes = masking.model_classes_for(full, masks)
    scl_clear = masking.clear_mask(full, None, cfg)
    combined = masking.clear_mask(full, classes, cfg)
    refl = cube.reflectance(full)

    out = data_dir / "reports" / aoi_id / "masking"
    (out / "img").mkdir(parents=True, exist_ok=True)
    rows, cards = [], []
    for t in range(full.sizes["time"]):
        item = str(full["item_id"].values[t])
        day = str(full["time"].values[t].astype("datetime64[D]"))
        rgb = _rgb(*(refl[b].values[t] for b in ("red", "green", "blue")))
        overlay = _tint(rgb, ~scl_clear[t], SCL_TINT)
        overlay = _tint(overlay, scl_clear[t] & ~combined[t], MODEL_TINT)
        _write_png(out / "img" / f"{item}.png", rgb)
        _write_png(out / "img" / f"{item}_masks.png", overlay)
        row = {
            "date": day,
            "item_id": item,
            "scl_clear": round(float(masking.clear_fraction(scl_clear[t], grid.mask)), 3),
            "combined_clear": round(float(masking.clear_fraction(combined[t], grid.mask)), 3),
        }
        rows.append(row)
        cards.append(
            f'<figure><img src="img/{item}.png"><img src="img/{item}_masks.png">'
            f"<figcaption><b>{day}</b> {html.escape(item)}<br>clear: SCL {row['scl_clear']:.0%}"
            f" · SCL+model {row['combined_clear']:.0%}</figcaption></figure>"
        )

    with (out / "passes.csv").open("w", newline="") as f:
        writer = csv.DictWriter(f, [*rows[0], "usable"] if rows else ["item_id", "usable"])
        writer.writeheader()
        writer.writerows({**r, "usable": ""} for r in rows)

    labels = read_labels(LABELS_DIR / f"{aoi_id}.csv")
    scores = score(rows, labels, cfg.scene_min_clear_frac) if labels else []
    summary = "".join(
        f"<li>{s.method}: lets through {s.let_through}, throws away {s.thrown_away} "
        f"of {s.labelled} labelled scenes</li>"
        for s in scores
    ) or (
        f"<li>No labels yet. Fill <code>usable</code> (yes/no) in passes.csv and copy the rows "
        f"to <code>eval/scene_usability/{html.escape(aoi_id)}.csv</code>.</li>"
    )
    (out / "index.html").write_text(
        f"""<!doctype html><meta charset="utf-8"><title>Masks: {html.escape(aoi_id)}</title>
<style>
:root{{color-scheme:light dark;font:14px system-ui,sans-serif}}
body{{margin:16px}} main{{display:flex;flex-wrap:wrap;gap:12px}}
figure{{margin:0}} img{{width:{2 * grid.width}px;image-rendering:pixelated;margin-right:2px}}
.k{{display:inline-block;width:10px;height:10px;margin:0 4px}}
</style>
<h1>Cloud masks: {html.escape(aoi_id)}</h1>
<p>Each pass: true colour, then the masks.
<span class="k" style="background:rgb({",".join(map(str, SCL_TINT))})"></span>SCL drops
<span class="k" style="background:rgb({",".join(map(str, MODEL_TINT))})"></span>only the
model drops. Dilated by {cfg.dilate_px} px. A scene counts as clear at
{cfg.scene_min_clear_frac:.0%} of the AOI.</p>
<ul>{summary}</ul>
<main>{"".join(cards)}</main>
"""
    )
    return out / "index.html", scores
