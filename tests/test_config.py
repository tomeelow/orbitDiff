import tomllib
from datetime import date

import pytest
from pydantic import ValidationError

from orbitdiff.config import REPO_ROOT, PipelineConfig, Settings, load_pipeline_config

PIPELINE_TOML = REPO_ROOT / "config" / "pipeline.toml"


@pytest.fixture
def raw() -> dict:
    with PIPELINE_TOML.open("rb") as f:
        return tomllib.load(f)


def test_repo_config_loads():
    cfg = load_pipeline_config(PIPELINE_TOML)
    assert cfg.aoi.archive_start == date(2017, 1, 1)
    assert cfg.aoi.max_per_user >= 1
    assert cfg.masking.scl_drop == {0, 1, 3, 8, 9, 10}
    assert 11 in cfg.masking.scl_flag  # snow is flagged, never silently dropped


def test_misspelt_key_is_rejected(raw):
    raw["masking"]["scl_dorp"] = raw["masking"].pop("scl_drop")
    with pytest.raises(ValidationError):
        PipelineConfig.model_validate(raw)


@pytest.mark.parametrize(
    ("drop", "flag"),
    [
        ([0, 1, 3, 12], [11]),  # 12 is not an SCL class
        ([0, 1, 3, 11], [11]),  # snow both dropped and flagged
    ],
)
def test_bad_scl_classes_are_rejected(raw, drop, flag):
    raw["masking"] |= {"scl_drop": drop, "scl_flag": flag}
    with pytest.raises(ValidationError):
        PipelineConfig.model_validate(raw)


def test_config_path_can_come_from_the_environment(monkeypatch, tmp_path):
    custom = tmp_path / "pipeline.toml"
    custom.write_bytes(PIPELINE_TOML.read_bytes().replace(b"max_km2 = 10", b"max_km2 = 2"))
    monkeypatch.setenv("ORBITDIFF_PIPELINE_CONFIG", str(custom))

    assert load_pipeline_config(Settings().pipeline_config).aoi.max_km2 == 2
