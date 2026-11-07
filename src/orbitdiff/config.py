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
BANDS = ("blue", "green", "red", "nir")  # the 10 m bands we read, in our names
ASSETS = frozenset(BANDS) | {"scl"}


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
    page_size: int = Field(ge=1)
    assets: dict[str, str]  # our band name -> the catalog's asset key
    token_url: str | None = None  # hrefs need a short-lived SAS token from here

    @model_validator(mode="after")
    def _maps_every_asset(self) -> Self:
        if set(self.assets) != ASSETS:
            raise ValueError(f"assets must map exactly {sorted(ASSETS)}")
        return self


class CatalogConfig(_Section):
    primary: Catalog
    fallback: Catalog


class ReadingConfig(_Section):
    max_tile_cloud_pct: float = Field(gt=0, le=100)
    fetch_min_clear_frac: float = Field(ge=0, le=1)
    workers: int = Field(ge=1)


class MaskingConfig(_Section):
    scl_drop: frozenset[int]
    scl_flag: frozenset[int]
    scene_min_clear_frac: float = Field(ge=0, le=1)
    dilate_px: int = Field(ge=0)
    cloud_model: str
    cloud_model_version: float
    cloud_model_classes: frozenset[int]

    @model_validator(mode="after")
    def _known_disjoint_classes(self) -> Self:
        unknown = (self.scl_drop | self.scl_flag) - SCL_CLASSES
        if unknown:
            raise ValueError(f"not SCL classes: {sorted(unknown)}")
        if both := self.scl_drop & self.scl_flag:
            raise ValueError(f"SCL classes both dropped and flagged: {sorted(both)}")
        return self


class CompositesConfig(_Section):
    fill_max_months: int = Field(ge=0)


class DetectionConfig(_Section):
    min_mapping_unit_ha: float = Field(gt=0)
    seasonal_revert_months: int = Field(ge=1)
    confirm_on_next_clear: bool


class JobsConfig(_Section):
    backfill_timeout_hours: float = Field(gt=0)


class ScheduleConfig(_Section):
    poll_every_hours: int = Field(ge=1)


class PipelineConfig(_Section):
    aoi: AOIConfig
    catalog: CatalogConfig
    reading: ReadingConfig
    masking: MaskingConfig
    composites: CompositesConfig
    detection: DetectionConfig
    jobs: JobsConfig
    schedule: ScheduleConfig


class Settings(BaseSettings):
    """Environment-specific values. Read from `.env` in development."""

    model_config = SettingsConfigDict(env_prefix="ORBITDIFF_", env_file=".env", extra="ignore")

    pipeline_config: Path = REPO_ROOT / "config" / "pipeline.toml"
    # Per-AOI cubes, composites and the STAC search cache. Never in git.
    data_dir: Path = REPO_ROOT / "data"
    # Development defaults match docker-compose.yml; deployments set them in the environment.
    database_url: str = "postgresql+psycopg://orbitdiff:orbitdiff@localhost:5433/orbitdiff"
    redis_url: str = "redis://localhost:6379/0"
    titiler_url: str = "http://localhost:8001"  # as the browser reaches it
    titiler_data_dir: str = "/data"  # where TiTiler sees data_dir
    cors_origins: list[str] = ["http://localhost:5173"]  # the web app's dev server


def load_pipeline_config(path: Path) -> PipelineConfig:
    with path.open("rb") as f:
        return PipelineConfig.model_validate(tomllib.load(f))


@cache
def settings() -> Settings:
    return Settings()


@cache
def pipeline_config() -> PipelineConfig:
    return load_pipeline_config(settings().pipeline_config)
