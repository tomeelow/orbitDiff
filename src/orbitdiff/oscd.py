"""OSCD, the Onera Satellite Change Detection dataset: 24 Sentinel-2 image pairs over
cities, with pixel labels of urban change and an official train/test split by city.

The images are L1C (top of atmosphere), 13 bands resampled to 10 m; only the four bands we
serve are read. Licence CC BY-NC-SA 4.0. Downloaded once into `data/oscd/`, each zip
checked against the SHA-256 that TorchGeo pins.
"""

import hashlib
import shutil
import urllib.parse
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import rasterio
from loguru import logger

from orbitdiff.config import BANDS, OscdConfig

CHUNK = 1 << 20
IMAGES = "Onera Satellite Change Detection dataset - Images"
LABELS = {
    "train": "Onera Satellite Change Detection dataset - Train Labels",
    "test": "Onera Satellite Change Detection dataset - Test Labels",
}
BAND_FILES = {"blue": "B02", "green": "B03", "red": "B04", "nir": "B08"}  # ours -> OSCD's
L1C_SCALE = 10_000  # digital numbers per unit of reflectance
CHANGED = 2  # in the label rasters; 1 is unchanged


@dataclass(frozen=True)
class City:
    name: str
    dates: tuple[str, str]  # "YYYYMMDD", before and after
    before: np.ndarray  # (band, y, x) top-of-atmosphere reflectance, bands as in BANDS
    after: np.ndarray
    changed: np.ndarray  # (y, x) bool


def cities(folder: Path, split: str) -> list[str]:
    """The official split: "train" or "test"."""
    return (folder / IMAGES / f"{split}.txt").read_text().strip().split(",")


def _image(city: Path, date: int) -> np.ndarray:
    layers = []
    for band in BANDS:
        with rasterio.open(city / f"imgs_{date}_rect" / f"{BAND_FILES[band]}.tif") as r:
            layers.append(r.read(1).astype(np.float32) / L1C_SCALE)
    return np.stack(layers)


def load_city(folder: Path, name: str, split: str) -> City:
    city = folder / IMAGES / name
    dates = tuple(
        line.split(":")[1].strip() for line in (city / "dates.txt").read_text().split("\n") if line
    )
    with rasterio.open(folder / LABELS[split] / name / "cm" / f"{name}-cm.tif") as r:
        changed = r.read(1) == CHANGED
    before, after = _image(city, 1), _image(city, 2)
    if not before.shape == after.shape == (len(BANDS), *changed.shape):
        raise ValueError(f"OSCD {name}: images and labels don't share a grid")
    return City(name, dates, before, after, changed)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        while block := f.read(CHUNK):
            digest.update(block)
    return digest.hexdigest()


def download(cfg: OscdConfig, folder: Path) -> Path:
    """Fetch and unpack the dataset into `folder`, once. A zip whose checksum doesn't match
    is deleted and refused."""
    folder.mkdir(parents=True, exist_ok=True)
    for name, sha256 in cfg.files.items():
        unpacked = folder / Path(name).stem
        if unpacked.is_dir():
            continue
        archive = folder / name
        if not archive.exists():
            logger.info("OSCD: downloading {}", name)
            partial = archive.with_suffix(".part")
            url = cfg.url + urllib.parse.quote(name)
            with urllib.request.urlopen(url) as r, partial.open("wb") as f:
                shutil.copyfileobj(r, f, CHUNK)
            partial.rename(archive)
        if _sha256(archive) != sha256:
            archive.unlink()
            raise ValueError(f"OSCD: {name} doesn't match its pinned SHA-256; deleted it")
        with zipfile.ZipFile(archive) as z:
            z.extractall(folder)  # each zip holds one folder, named like it
        archive.unlink()
    return folder


def version(cfg: OscdConfig) -> str:
    """The dataset's version: its files' checksums, shortened."""
    return hashlib.sha256("".join(sorted(cfg.files.values())).encode()).hexdigest()[:12]
