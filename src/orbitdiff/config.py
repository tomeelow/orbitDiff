"""Pipeline parameters from `config/pipeline.toml`; secrets and paths from the environment.

The rest of the package reads configuration only through `pipeline_config()` and
`settings()`, never from the file or the environment directly.
"""

import tomllib
from datetime import date
from functools import cache
from pathlib import Path
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[2]
SCL_CLASSES = frozenset(range(12))  # Sentinel-2 L2A scene classification: 0..11


class _Section(BaseModel):
    # A misspelt key must fail loudly, not silently fall back to nothing.
    model_config = ConfigDict(extra="forbid", frozen=True)


class AOIConfig(_Section):
    max_km2: float = Field(gt=0)
    archive_start: date


class Catalog(_Section):
    name: str
    url: str
    collection: str


class CatalogConfig(_Section):
    primary: Catalog
    fallback: Catalog


class MaskingConfig(_Section):
    scl_drop: frozenset[int]
    scl_flag: frozenset[int]
    scene_min_clear_frac: float = Field(ge=0, le=1)

    @model_validator(mode="after")
    def _known_disjoint_classes(self) -> Self:
        unknown = (self.scl_drop | self.scl_flag) - SCL_CLASSES
        if unknown:
            raise ValueError(f"not SCL classes: {sorted(unknown)}")
        if both := self.scl_drop & self.scl_flag:
            raise ValueError(f"SCL classes both dropped and flagged: {sorted(both)}")
        return self


class DetectionConfig(_Section):
    min_mapping_unit_ha: float = Field(gt=0)
    seasonal_revert_months: int = Field(ge=1)
    confirm_on_next_clear: bool


class ScheduleConfig(_Section):
    poll_every_hours: int = Field(ge=1)


class PipelineConfig(_Section):
    aoi: AOIConfig
    catalog: CatalogConfig
    masking: MaskingConfig
    detection: DetectionConfig
    schedule: ScheduleConfig


class Settings(BaseSettings):
    """Environment-specific values. Read from `.env` in development."""

    model_config = SettingsConfigDict(env_prefix="ORBITDIFF_", env_file=".env", extra="ignore")

    pipeline_config: Path = REPO_ROOT / "config" / "pipeline.toml"


def load_pipeline_config(path: Path) -> PipelineConfig:
    with path.open("rb") as f:
        return PipelineConfig.model_validate(tomllib.load(f))


@cache
def settings() -> Settings:
    return Settings()


@cache
def pipeline_config() -> PipelineConfig:
    return load_pipeline_config(settings().pipeline_config)
