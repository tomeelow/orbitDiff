"""Which trained building model the product runs: the champion.

A model becomes champion only through `orbitdiff promote`, and only if it passes the gate
on a verified evaluation set (`evaluation.gate`). Promotion is never automatic. Its
weights and version sit in `data/models/buildings/`, next to the cloud model's, so every
worker, local or in Docker, runs the same one. The MLflow registry records the same
choice as the `champion` alias.

No PyTorch here: the API imports the pipeline but never runs a model. Until something is
promoted, nothing needs the `ml` group.
"""

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import numpy as np

CHAMPION = "champion"  # the file stem here, and the alias in the MLflow registry


def folder(data_dir: Path) -> Path:
    return data_dir / "models" / "buildings"


def champion_info(data_dir: Path) -> dict | None:
    path = folder(data_dir) / f"{CHAMPION}.json"
    return json.loads(path.read_text()) if path.exists() else None


@dataclass(frozen=True)
class Named:
    """The champion known by its version only, to name the method it runs in where no
    model runs (the admin view)."""

    version: str

    def __call__(self, before: np.ndarray, after: np.ndarray) -> np.ndarray:
        raise RuntimeError(f"{self.version} isn't loaded here")


def champion_named(data_dir: Path) -> Named | None:
    info = champion_info(data_dir)
    return None if info is None else Named(f"{info['registered_model']}/{info['version']}")


def load_champion(data_dir: Path):  # -> siamese.Predictor | None, without importing torch
    """The champion as a predictor, or None while there is none. A champion without the
    `ml` group fails loudly: running without it would quietly change what alerts."""
    info = champion_info(data_dir)
    if info is None:
        return None
    from orbitdiff import siamese

    return siamese.load_weights(
        folder(data_dir) / info["weights"], f"{info['registered_model']}/{info['version']}"
    )


def write_champion(data_dir: Path, registered_model: str, version: int, run_id: str) -> Path:
    """Record a promoted model; its weights must already be in `folder(data_dir)`."""
    path = folder(data_dir) / f"{CHAMPION}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    info = {
        "registered_model": registered_model,
        "version": version,
        "run_id": run_id,
        "weights": f"v{version}.pt",
        "promoted_utc": datetime.now(UTC).isoformat(timespec="seconds"),
    }
    partial = path.with_suffix(".part")
    partial.write_text(json.dumps(info, indent=2) + "\n")
    partial.replace(path)  # a worker never reads half a file
    return path
