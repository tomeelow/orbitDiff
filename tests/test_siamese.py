"""The Siamese U-Net: shapes, the input standardisation that bridges L1C and L2A, missing
pixels, and weights that load back to the same model. Needs the `ml` group (PyTorch)."""

import numpy as np
import pytest
import rasterio

torch = pytest.importorskip("torch")

from torch import nn  # noqa: E402

from orbitdiff import buildings, oscd, retraining, siamese  # noqa: E402
from orbitdiff.config import pipeline_config, settings  # noqa: E402
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


def test_the_loss_ignores_pixels_nobody_labelled():
    logits = torch.tensor([[[2.0, 50.0]]], requires_grad=True)
    loss = siamese.patch_loss(logits, torch.tensor([[[0.0, float("nan")]]]), pos_weight=3.0)
    alone = siamese.patch_loss(torch.tensor([[[2.0]]]), torch.tensor([[[0.0]]]), pos_weight=3.0)
    assert loss.item() == pytest.approx(alone.item())
    loss.backward()
    assert logits.grad[0, 0, 1] == 0


def test_fully_labelled_patches_give_plain_weighted_cross_entropy():
    logits, labels = torch.randn(4, 8, 8), torch.randint(0, 2, (4, 8, 8)).float()
    plain = nn.BCEWithLogitsLoss(pos_weight=torch.tensor(5.0))(logits, labels)
    assert siamese.patch_loss(logits, labels, pos_weight=5.0).item() == pytest.approx(plain.item())


def test_a_patch_weighs_the_same_however_few_pixels_it_has_labels_for():
    nan = float("nan")
    logits = torch.tensor([[[1.0, 1.0]], [[3.0, -9.0]]])
    labels = torch.tensor([[[0.0, 0.0]], [[0.0, nan]]])
    per_patch = [
        siamese.patch_loss(logits[:1], labels[:1], pos_weight=1.0),
        siamese.patch_loss(logits[1:], labels[1:], pos_weight=1.0),
    ]
    together = siamese.patch_loss(logits, labels, pos_weight=1.0)
    assert together.item() == pytest.approx((per_patch[0].item() + per_patch[1].item()) / 2)


def test_a_patch_with_no_labels_adds_nothing_and_is_not_nan():
    labels = torch.full((1, 4, 4), float("nan"))
    assert siamese.patch_loss(torch.zeros(1, 4, 4), labels, pos_weight=1.0).item() == 0


def test_a_hard_negative_labels_only_the_footprint_standardised_over_the_whole_area():
    before, after = images(height=200, width=220)
    before[:, :20, :] = np.nan  # unseen somewhere away from the footprint
    footprint = np.zeros((200, 220), bool)
    footprint[100:110, 50:60] = True

    pair, labels = siamese.hard_negative(before, after, footprint, patch_px=96)

    assert labels.shape == (96, 96) and pair.shape == (8, 96, 96)
    assert (labels == 0).sum() == 100 and np.isnan(labels).sum() == 96**2 - 100
    whole_before, whole_after, _ = siamese.standardize(before, after)
    y = np.nonzero(np.any(labels == 0, axis=1))[0][0] - 100
    x = np.nonzero(np.any(labels == 0, axis=0))[0][0] - 50
    np.testing.assert_array_equal(pair[0], whole_before[0, -y : -y + 96, -x : -x + 96])
    np.testing.assert_array_equal(pair[4], whole_after[0, -y : -y + 96, -x : -x + 96])


def test_a_footprint_bigger_than_a_patch_is_kept_whole_and_one_at_the_edge_stays_on_the_image():
    before, after = images(height=200, width=220)
    big = np.zeros((200, 220), bool)
    big[10:150, 0:20] = True
    pair, labels = siamese.hard_negative(before, after, big, patch_px=96)
    assert labels.shape == (140, 96) and (labels == 0).sum() == big.sum()

    corner = np.zeros((200, 220), bool)
    corner[-3:, -3:] = True
    _, labels = siamese.hard_negative(before, after, corner, patch_px=96)
    assert labels.shape == (96, 96) and (labels == 0).sum() == 9


def test_an_area_smaller_than_a_patch_is_padded_and_the_padding_has_no_labels():
    before, after = images(height=45, width=70)
    footprint = np.zeros((45, 70), bool)
    footprint[20:25, 30:35] = True
    pair, labels = siamese.hard_negative(before, after, footprint, patch_px=96)
    assert labels.shape == (96, 96) and pair.shape == (8, 96, 96)
    assert (labels == 0).sum() == 25 and np.isnan(labels[45:, :]).all()


def test_a_footprint_pixel_not_seen_on_both_dates_gets_no_label():
    before, after = images(height=100, width=100)
    footprint = np.zeros((100, 100), bool)
    footprint[50:52, 50:52] = True
    after[:, 50, 50] = np.nan
    _, labels = siamese.hard_negative(before, after, footprint, patch_px=96)
    assert (labels == 0).sum() == 3


def write_oscd(folder, size=64):
    """A tiny OSCD: three train cities (one for validation) and one test city."""
    rng = np.random.default_rng(0)
    images_dir = folder / oscd.IMAGES
    images_dir.mkdir(parents=True)
    (images_dir / "train.txt").write_text("a,b,val")
    (images_dir / "test.txt").write_text("t")
    profile = {"driver": "GTiff", "width": size, "height": size, "count": 1}
    for split, names in (("train", ("a", "b", "val")), ("test", ("t",))):
        for name in names:
            city = images_dir / name
            (city / "dates.txt").parent.mkdir(parents=True, exist_ok=True)
            (city / "dates.txt").write_text("date_1: 20170101\ndate_2: 20180101\n")
            for date in (1, 2):
                for band in oscd.BAND_FILES.values():
                    (city / f"imgs_{date}_rect").mkdir(exist_ok=True)
                    data = rng.integers(500, 3000, (size, size), "uint16")
                    with rasterio.open(
                        city / f"imgs_{date}_rect" / f"{band}.tif", "w", dtype="uint16", **profile
                    ) as dst:
                        dst.write(data, 1)
            labels = np.ones((size, size), "uint8")
            labels[10:20, 10:20] = oscd.CHANGED
            label_dir = folder / oscd.LABELS[split] / name / "cm"
            label_dir.mkdir(parents=True)
            with rasterio.open(label_dir / f"{name}-cm.tif", "w", dtype="uint8", **profile) as dst:
                dst.write(labels, 1)


@pytest.fixture
def tiny_cfg():
    cfg = pipeline_config().buildings
    return cfg.model_copy(
        update={
            "val_cities": ("val",),
            "steps": 2,
            "batch": 4,
            "feedback_per_batch": 2,
            "patch_px": 32,
        }
    )


def not_real(event_id, aoi_id, seed=0):
    before, after = images(height=60, width=60, seed=seed)
    footprint = np.zeros((60, 60), bool)
    footprint[20:30, 20:30] = True
    return retraining.NotReal(event_id, aoi_id, before, after, footprint)


def test_feedback_is_split_by_whole_areas_and_a_lone_area_is_all_trained_on():
    items = [not_real(i, area) for i, area in enumerate([1, 1, 2, 3, 3, 4, 5, 6, 6, 7])]
    to_train, held_out = retraining.split(items, every=5)
    assert {i.aoi_id for i in held_out} == {1, 6}
    assert not {i.aoi_id for i in held_out} & {i.aoi_id for i in to_train}
    assert len(to_train) + len(held_out) == len(items)

    lone = [not_real(0, 9), not_real(1, 9)]
    assert retraining.split(lone, every=5) == (lone, [])


def test_false_alarms_count_only_footprint_pixels_the_model_could_see():
    item = not_real(0, 1)

    def all_changed(before, after):
        return np.ones(item.footprint.shape)

    def half_unseen(before, after):
        out = np.ones(item.footprint.shape)
        out[20:25, :] = np.nan
        out[25:, :] = 0
        return out

    assert retraining.false_alarm_share(all_changed, [item], 0.5) == 1
    assert retraining.false_alarm_share(half_unseen, [item], 0.5) == 0
    assert retraining.false_alarm_share(all_changed, [], 0.5) is None


def test_a_candidate_is_registered_with_its_data_and_never_promoted(
    tmp_path, monkeypatch, tiny_cfg
):
    mlflow = pytest.importorskip("mlflow")
    monkeypatch.setenv("MLFLOW_DISABLE_TELEMETRY", "true")
    monkeypatch.setenv("ORBITDIFF_MLFLOW_TRACKING_URI", f"sqlite:///{tmp_path}/m/mlflow.db")
    settings.cache_clear()
    write_oscd(tmp_path / "oscd")
    monkeypatch.setattr(oscd, "download", lambda cfg, folder: folder)
    feedback = [not_real(1, 10), not_real(2, 20), not_real(3, 20)]

    candidate = retraining.train_candidate(tiny_cfg, tmp_path, feedback)
    settings.cache_clear()

    assert (candidate.trained_on, candidate.held_out) == (2, 1)  # area 10 is held out
    assert candidate.version == 1 and candidate.false_alarms is not None
    assert not buildings.folder(tmp_path).exists()  # nothing here says it runs
    params = mlflow.get_run(candidate.run_id).data.params
    assert params["feedback_trained_on"] == "2" and "+" in params["dataset_version"]
    assert params["feedback_held_out"] == "1"
    artifacts = mlflow.get_experiment_by_name("buildings").artifact_location
    assert artifacts == (tmp_path / "mlruns").resolve().as_uri()


def test_without_feedback_training_is_oscd_alone_and_repeatable(tmp_path, tiny_cfg):
    write_oscd(tmp_path / "oscd")
    first = siamese.train(tiny_cfg, tmp_path / "oscd")
    again = siamese.train(tiny_cfg, tmp_path / "oscd")
    assert [h[1] for h in first.history] == [h[1] for h in again.history]
