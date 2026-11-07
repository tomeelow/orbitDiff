"""Postgres + PostGIS records (ADR-006): AOIs, the passes over them, their months, and the
changes found in them.

Rasters stay in the data store; these rows describe them. Geometries are stored in
EPSG:4326 and every area is computed in the AOI's UTM zone. Accounts arrive in Phase 9,
and with them an owner on every AOI.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date, datetime
from functools import cache

from geoalchemy2 import Geometry, WKBElement
from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    Engine,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    create_engine,
    false,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

from orbitdiff.config import settings

STAGES = ("queued", "reading", "masking", "compositing", "detecting", "ready", "failed")


class Base(DeclarativeBase):
    pass


class Aoi(Base):
    __tablename__ = "aois"
    __table_args__ = (Index("ix_aois_geometry", "geometry", postgresql_using="gist"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(80))
    geometry: Mapped[WKBElement] = mapped_column(
        Geometry("POLYGON", srid=4326, spatial_index=False)
    )
    epsg: Mapped[int] = mapped_column(Integer)  # the UTM zone everything is computed in
    area_ha: Mapped[float] = mapped_column(Float)
    archive_start: Mapped[date]
    created_utc: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    # Backfill progress, kept here so it outlives the job: "Indexed 412 of 1,180 scenes".
    stage: Mapped[str] = mapped_column(String(16), default="queued")
    progress_done: Mapped[int] = mapped_column(Integer, default=0)
    progress_total: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(String, default=None)


class Scene(Base):
    """One pass over one AOI: the item that stood for it and what was read."""

    __tablename__ = "scenes"

    aoi_id: Mapped[int] = mapped_column(ForeignKey("aois.id", ondelete="CASCADE"), primary_key=True)
    item_id: Mapped[str] = mapped_column(String, primary_key=True)  # the provenance anchor
    catalog: Mapped[str] = mapped_column(String(32))
    acquired_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    platform: Mapped[str] = mapped_column(String(16))
    mgrs_tile: Mapped[str] = mapped_column(String(8))
    processing_baseline: Mapped[str] = mapped_column(String(8))
    tile_cloud_pct: Mapped[float] = mapped_column(Float)
    read_status: Mapped[str] = mapped_column(String(16))  # full | scl | tile_cloud
    # SCL + cloud model over this AOI when the bands were read; SCL alone when the pass
    # was too cloudy to read them; unknown when skipped on tile cloud.
    aoi_clear_frac: Mapped[float | None] = mapped_column(Float)
    # Its offset flag contradicts its pixels, so no composite uses it (docs/spikes.md, 5).
    offset_suspect: Mapped[bool] = mapped_column(Boolean, server_default=false())
    bytes_read: Mapped[int] = mapped_column(BigInteger)
    full_bytes: Mapped[int] = mapped_column(BigInteger)


class Month(Base):
    """One monthly composite of one AOI."""

    __tablename__ = "months"

    aoi_id: Mapped[int] = mapped_column(ForeignKey("aois.id", ondelete="CASCADE"), primary_key=True)
    month: Mapped[date] = mapped_column(primary_key=True)  # first day, UTC
    passes: Mapped[int] = mapped_column(Integer)
    with_bands: Mapped[int] = mapped_column(Integer)
    clear_scenes: Mapped[int] = mapped_column(Integer)
    observed_frac: Mapped[float] = mapped_column(Float)  # AOI pixels with a clear observation
    filled_frac: Mapped[float] = mapped_column(Float)  # AOI pixels filled for display only
    ndvi_mean: Mapped[float | None] = mapped_column(Float)  # over observed pixels only
    version: Mapped[str] = mapped_column(String(16))  # changes whenever its tiles do


class Event(Base):
    """A change in an AOI, with everything needed to explain it to a user or debug it: the
    months compared, the passes behind it, every status and what caused it, and the method
    version and thresholds in force (CLAUDE.md, provenance)."""

    __tablename__ = "events"
    __table_args__ = (Index("ix_events_geometry", "geometry", postgresql_using="gist"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    aoi_id: Mapped[int] = mapped_column(ForeignKey("aois.id", ondelete="CASCADE"), index=True)
    month: Mapped[date]  # the composite month it first showed in
    reference_month: Mapped[date]  # the same month a year earlier
    category: Mapped[str] = mapped_column(String(24))
    status: Mapped[str] = mapped_column(String(16))  # pending | confirmed | dismissed | seasonal
    area_ha: Mapped[float] = mapped_column(Float)
    geometry: Mapped[WKBElement] = mapped_column(
        Geometry("GEOMETRY", srid=4326, spatial_index=False)
    )
    ndvi_before: Mapped[float | None] = mapped_column(Float)
    ndvi_after: Mapped[float | None] = mapped_column(Float)
    detected_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True))  # first seen
    status_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True))  # current status since
    history: Mapped[list] = mapped_column(JSON)  # [{"status", "cause", "at"}], oldest first
    items: Mapped[list] = mapped_column(JSON)  # STAC item ids clear over it in both months
    method_version: Mapped[str] = mapped_column(String(48))
    thresholds: Mapped[dict] = mapped_column(JSON)  # the detection settings in force
    updated_utc: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


@cache
def engine() -> Engine:
    return create_engine(settings().database_url, pool_pre_ping=True)


@contextmanager
def session() -> Iterator[Session]:
    """A session that commits on success and rolls back on error."""
    with sessionmaker(engine(), expire_on_commit=False).begin() as s:
        yield s
