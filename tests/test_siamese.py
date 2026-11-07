"""The Siamese U-Net: shapes, the input standardisation that bridges L1C and L2A, missing
pixels, and weights that load back to the same model. Needs the `ml` group (PyTorch)."""

import numpy as np
import pytest

torch = pytest.importorskip("torch")

from orbitdiff import buildings, siamese  # noqa: E402
from orbitdiff.detection import Learned  # noqa: E402


@pytest.fixture
def predictor():
    torch.manual_seed(0)
    return siamese.Predictor(siamese.SiamDiff(), "orbitdiff_buildings/1")


def images(height=45, width=70, seed=0):
    rng = np.random.default_rng(seed)
    before = rng.uniform(0.02, 0.3, (4, height, width)).astype(np.float32)
    after = before + rng.normal(0, 0.01, before.shape).astype(np.float32)
    return before, after


def test_the_model_maps_a_stacked_pair_to_one_logit_per_pixel():
    model = siamese.SiamDiff()
    assert model(torch.zeros(2, 8, 64, 96)).shape == (2, 64, 96)


def test_standardising_takes_out_a_per_band_offset_and_gain():
    """Top-of-atmosphere reflectance is roughly surface reflectance times a gain plus path
    radiance, per band: standardised, the two look the same to the model."""
    before, after = images()
    gain = np.array([0.9, 0.95, 1.0, 1.05], np.float32)[:, None, None]
    haze = np.array([0.08, 0.05, 0.03, 0.01], np.float32)[:, None, None]

    surface = siamese.standardize(before, after)
    top_of_atmosphere = siamese.standardize(before * gain + haze, after * gain + haze)

    np.testing.assert_allclose(surface[0], top_of_atmosphere[0], atol=1e-4)
    np.testing.assert_allclose(surface[1], top_of_atmosphere[1], atol=1e-4)


def test_standardising_uses_only_pixels_seen_on_both_dates():
    before, after = images()
    after[:, :10] = np.nan  # a cloud on the second date
    after[:, 10:20] += 5.0  # bright pixels the stats must not ignore: they are seen

    b, a, seen = siamese.standardize(before, after)

    assert not seen[:10].any() and seen[10:].all()
    assert (b[:, :10] == 0).all() and (a[:, :10] == 0).all()
    np.testing.assert_allclose(b[:, seen].mean(axis=1), 0, atol=1e-5)
    np.testing.assert_allclose(a[:, seen].std(axis=1), 1, atol=1e-4)


def test_the_predictor_returns_a_probability_per_pixel_and_nan_where_unseen(predictor):
    before, after = images(height=45, width=70)  # not multiples of 16: padded, cropped back
    before[:, 30:, 50:] = np.nan

    p = predictor(before, after)

    assert p.shape == (45, 70)
    assert np.isnan(p[30:, 50:]).all()
    seen = p[~np.isnan(p)]
    assert seen.size == 45 * 70 - 15 * 20 and ((seen >= 0) & (seen <= 1)).all()


def test_too_little_seen_on_both_dates_gives_no_probability(predictor):
    before, after = images()
    after[:, 3:] = np.nan  # 3 rows x 70 px seen: below MIN_SEEN_PX
    assert np.isnan(predictor(before, after)).all()


def test_as_a_detector_missing_pixels_never_change(predictor):
    before, after = images()
    after[:, :5] = np.nan
    changed = Learned(predictor, threshold=0.0).changed(before, after)
    assert changed.dtype == bool and not changed[:5].any() and changed[5:].all()


def test_saved_weights_load_back_as_the_same_model(tmp_path, predictor):
    before, after = images()
    path = buildings.folder(tmp_path) / "v1.pt"
    siamese.save_weights(predictor, path)
    buildings.write_champion(tmp_path, "orbitdiff_buildings", 1, "run-1")

    loaded = buildings.load_champion(tmp_path)

    assert loaded.version == "orbitdiff_buildings/1"
    np.testing.assert_array_equal(loaded(before, after), predictor(before, after))


def test_pixel_scores_add_up_over_images():
    total = siamese.Scores(3, 1, 2) + siamese.Scores(1, 1, 0)
    assert (total.precision, total.recall) == (4 / 6, 4 / 6)
    assert total.f1 == pytest.approx(2 * 4 / (2 * 4 + 2 + 2))
    assert siamese.Scores(0, 0, 0).f1 == 0


def test_a_logged_model_comes_back_from_the_registry_for_any_area_size(tmp_path, monkeypatch):
    mlflow = pytest.importorskip("mlflow")
    monkeypatch.setenv("MLFLOW_DISABLE_TELEMETRY", "true")
    mlflow.set_tracking_uri(f"sqlite:///{tmp_path / 'mlflow.db'}")
    mlflow.create_experiment("test", artifact_location=(tmp_path / "artifacts").as_uri())
    mlflow.set_experiment("test")
    torch.manual_seed(0)
    predictor = siamese.Predictor(siamese.SiamDiff(), "candidate")
    with mlflow.start_run():
        info = siamese.log_model(predictor, "orbitdiff_buildings")

    loaded = siamese.from_registry("orbitdiff_buildings", int(info.registered_model_version))

    assert loaded.version == "orbitdiff_buildings/1"
    before, after = images(height=45, width=70)  # not the size it was logged with
    np.testing.assert_array_equal(loaded(before, after), predictor(before, after))
