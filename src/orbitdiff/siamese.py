"""The learned building detector: a Siamese U-Net (FC-Siam-diff, Daudt et al., 2018),
trained on OSCD and used as one more detector in `orbitdiff.detection`.

One encoder reads both dates. At every scale the decoder sees the absolute difference of
their features, so a change is what the two dates disagree on, not what either looks like.

Train-serve skew: OSCD is L1C (top of atmosphere), we serve L2A (surface reflectance).
Each image's bands are standardised over the pixels seen on both dates before the model
sees them, which takes out most of the level difference between the two (and between a
hazy and a clear day). What is left is measured in docs/spikes.md, section 6.

Needs the `ml` group (PyTorch).
"""

import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import torch
from loguru import logger
from torch import nn
from torch.nn import functional

from orbitdiff import oscd
from orbitdiff.config import BANDS, BuildingsConfig

WIDTHS = (16, 32, 64, 128)  # encoder channels per scale, as in the paper
CONVS = (2, 2, 3, 3)  # convolutions per encoder scale; the decoder mirrors them
DROPOUT = 0.2
SCALE = 2 ** len(WIDTHS)  # inputs are padded to a multiple of this
MIN_SEEN_PX = 256  # fewer pixels seen on both dates than this: too few to standardise on
EVAL_EVERY = 250  # optimiser steps between validation scores


def _block(channels_in: int, channels_out: int, convs: int) -> nn.Sequential:
    layers: list[nn.Module] = []
    for i in range(convs):
        layers += [
            nn.Conv2d(channels_in if i == 0 else channels_out, channels_out, 3, padding=1),
            nn.BatchNorm2d(channels_out),
            nn.ReLU(inplace=True),
            nn.Dropout2d(DROPOUT),
        ]
    return nn.Sequential(*layers)


class SiamDiff(nn.Module):
    """(batch, 2 x band, y, x), the standardised before and after stacked on the channel
    axis, to change logits (batch, y, x). y and x are multiples of SCALE."""

    def __init__(self, bands: int = len(BANDS)) -> None:
        super().__init__()
        self.bands = bands
        widths_in = (bands, *WIDTHS[:-1])
        self.encoder = nn.ModuleList(
            _block(c_in, c, n) for c_in, c, n in zip(widths_in, WIDTHS, CONVS, strict=True)
        )
        up = WIDTHS[::-1]  # 128, 64, 32, 16
        out = (*WIDTHS[-2::-1], WIDTHS[0])  # 64, 32, 16, 16
        self.up = nn.ModuleList(nn.ConvTranspose2d(c, c, 2, stride=2) for c in up)
        self.decoder = nn.ModuleList(
            _block(2 * c, o, n) for c, o, n in zip(up, out, CONVS[::-1], strict=True)
        )
        self.head = nn.Conv2d(WIDTHS[0], 1, 1)

    def _encode(self, image: torch.Tensor) -> tuple[torch.Tensor, list[torch.Tensor]]:
        skips = []
        for block in self.encoder:
            image = block(image)
            skips.append(image)
            image = functional.max_pool2d(image, 2)
        return image, skips

    def forward(self, pair: torch.Tensor) -> torch.Tensor:
        x, before = self._encode(pair[:, : self.bands])
        _, after = self._encode(pair[:, self.bands :])
        for up, block, b, a in zip(self.up, self.decoder, before[::-1], after[::-1], strict=True):
            x = block(torch.cat([up(x), (a - b).abs()], dim=1))
        return self.head(x)[:, 0]


def standardize(before: np.ndarray, after: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Both images (band, y, x) with each band at zero mean and unit variance over the
    pixels seen on both dates, 0 elsewhere; and that mask (y, x)."""
    seen = np.isfinite(before).all(axis=0) & np.isfinite(after).all(axis=0)
    out = []
    for image in (before, after):
        values = image[:, seen]
        mean = values.mean(axis=1)[:, None, None] if seen.any() else 0.0
        std = values.std(axis=1)[:, None, None] if seen.any() else 1.0
        out.append(np.where(seen, (image - mean) / np.maximum(std, 1e-6), 0.0).astype(np.float32))
    return out[0], out[1], seen


@dataclass
class Predictor:
    """A trained model as a probability map: P(change) per pixel (y, x) between two images
    (band, y, x) of reflectance on one grid, NaN where either is missing."""

    model: SiamDiff
    version: str  # "<registered model>/<version>", recorded on every event it finds

    def __post_init__(self) -> None:
        self.model.eval()

    def __call__(self, before: np.ndarray, after: np.ndarray) -> np.ndarray:
        b, a, seen = standardize(before, after)
        if seen.sum() < MIN_SEEN_PX:
            return np.full(seen.shape, np.nan)
        height, width = seen.shape
        pad = ((0, 0), (0, -height % SCALE), (0, -width % SCALE))
        pair = torch.from_numpy(np.concatenate([np.pad(b, pad), np.pad(a, pad)]))[None]
        with torch.inference_mode():
            logits = self.model(pair)[0, :height, :width]
        return np.where(seen, torch.sigmoid(logits).numpy(), np.nan)


def save_weights(predictor: Predictor, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    torch.save(predictor.model.state_dict(), path)


def load_weights(path: Path, version: str) -> Predictor:
    model = SiamDiff()
    model.load_state_dict(torch.load(path, weights_only=True))
    return Predictor(model, version)


def log_model(predictor: Predictor, registered_model: str):  # -> mlflow ModelInfo
    """Log the model to the active MLflow run, with a signature, as a new registered
    version."""
    import mlflow
    from mlflow.models import infer_signature

    example = np.zeros((1, 2 * predictor.model.bands, 4 * SCALE, 4 * SCALE), np.float32)
    with torch.inference_mode():
        output = predictor.model(torch.from_numpy(example)).numpy()
    return mlflow.pytorch.log_model(
        predictor.model,
        name="model",
        registered_model_name=registered_model,
        signature=infer_signature(example, output),
        # Not "pt2": a traced graph fixes the input size, and every AOI has its own.
        serialization_format="pickle",
    )


def from_registry(registered_model: str, version: int) -> Predictor:
    import mlflow

    model = mlflow.pytorch.load_model(f"models:/{registered_model}/{version}")
    return Predictor(model, f"{registered_model}/{version}")


# Training


@dataclass(frozen=True)
class Scores:
    """Pixel scores of the change class, summed over images."""

    tp: int
    fp: int
    fn: int

    @property
    def precision(self) -> float:
        return self.tp / max(self.tp + self.fp, 1)

    @property
    def recall(self) -> float:
        return self.tp / max(self.tp + self.fn, 1)

    @property
    def f1(self) -> float:
        return 2 * self.tp / max(2 * self.tp + self.fp + self.fn, 1)

    def __add__(self, other: "Scores") -> "Scores":
        return Scores(self.tp + other.tp, self.fp + other.fp, self.fn + other.fn)


def score(predictor: Predictor, city: oscd.City, threshold: float) -> Scores:
    found = predictor(city.before, city.after) > threshold
    return Scores(
        int((found & city.changed).sum()),
        int((found & ~city.changed).sum()),
        int((~found & city.changed).sum()),
    )


def _patches(
    pairs_in: list[np.ndarray],
    labels_in: list[np.ndarray],
    cfg: BuildingsConfig,
    rng: np.random.Generator,
) -> tuple[torch.Tensor, torch.Tensor]:
    """A batch of random crops, rotated and flipped, from images weighted by their size."""
    sizes = np.array([label.size for label in labels_in], dtype=float)
    pairs, labels = [], []
    for i in rng.choice(len(pairs_in), size=cfg.batch, p=sizes / sizes.sum()):
        height, width = labels_in[i].shape
        y, x = rng.integers(height - cfg.patch_px + 1), rng.integers(width - cfg.patch_px + 1)
        window = np.s_[y : y + cfg.patch_px, x : x + cfg.patch_px]
        pair, label = pairs_in[i][(slice(None), *window)], labels_in[i][window]
        k = int(rng.integers(4))
        pair, label = np.rot90(pair, k, axes=(1, 2)), np.rot90(label, k)
        if rng.random() < 0.5:
            pair, label = pair[:, :, ::-1], label[:, ::-1]
        pairs.append(pair.copy())
        labels.append(label.copy())
    return torch.from_numpy(np.stack(pairs)), torch.from_numpy(np.stack(labels)).float()


@dataclass
class Trained:
    predictor: Predictor
    val: Scores
    test: dict[str, Scores]  # per test city
    pos_weight: float
    best_step: int
    seconds: float
    history: list[tuple[int, float, float]]  # step, training loss, validation F1


def train(cfg: BuildingsConfig, folder: Path) -> Trained:
    """Train on OSCD's train cities less `val_cities`, keep the step that scores best on
    the validation cities, and score that on the test cities, which nothing else sees."""
    t0 = time.perf_counter()
    torch.manual_seed(cfg.seed)
    torch.use_deterministic_algorithms(True)
    rng = np.random.default_rng(cfg.seed)
    official = oscd.cities(folder, "train")
    if not set(cfg.val_cities) < set(official):
        raise ValueError(f"val_cities must be some of OSCD's train cities: {official}")
    train_cities = [oscd.load_city(folder, n, "train") for n in official if n not in cfg.val_cities]
    val_cities = [oscd.load_city(folder, n, "train") for n in cfg.val_cities]
    train_pairs = [np.concatenate(standardize(c.before, c.after)[:2]) for c in train_cities]
    train_labels = [c.changed for c in train_cities]

    changed = sum(int(label.sum()) for label in train_labels)
    total = sum(label.size for label in train_labels)
    # Changed pixels are ~2%: weigh them up, by the square root of the imbalance so the
    # model doesn't learn to see change everywhere.
    pos_weight = float(np.sqrt((total - changed) / changed))
    model = SiamDiff()
    optimiser = torch.optim.Adam(model.parameters(), lr=cfg.learning_rate, weight_decay=1e-4)
    schedule = torch.optim.lr_scheduler.CosineAnnealingLR(optimiser, cfg.steps)
    loss_fn = nn.BCEWithLogitsLoss(pos_weight=torch.tensor(pos_weight))
    predictor = Predictor(model, "candidate")

    best: tuple[float, int, dict] = (-1.0, 0, {})
    history, losses = [], []
    for step in range(1, cfg.steps + 1):
        model.train()
        pairs, labels = _patches(train_pairs, train_labels, cfg, rng)
        loss = loss_fn(model(pairs), labels)
        optimiser.zero_grad()
        loss.backward()
        optimiser.step()
        schedule.step()
        losses.append(loss.item())
        if step % EVAL_EVERY == 0 or step == cfg.steps:
            model.eval()
            val = sum((score(predictor, c, cfg.threshold) for c in val_cities), Scores(0, 0, 0))
            history.append((step, float(np.mean(losses)), val.f1))
            logger.info("step {}: loss {:.4f}, validation F1 {:.3f}", step, np.mean(losses), val.f1)
            losses = []
            if val.f1 > best[0]:
                best = (val.f1, step, {k: v.clone() for k, v in model.state_dict().items()})

    model.load_state_dict(best[2])
    predictor = Predictor(model, "candidate")
    val = sum((score(predictor, c, cfg.threshold) for c in val_cities), Scores(0, 0, 0))
    test = {
        name: score(predictor, oscd.load_city(folder, name, "test"), cfg.threshold)
        for name in oscd.cities(folder, "test")
    }
    return Trained(predictor, val, test, pos_weight, best[1], time.perf_counter() - t0, history)
