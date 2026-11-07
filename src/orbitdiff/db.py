"""Postgres + PostGIS records (ADR-006): AOIs, the passes over them, their months, the
changes found in them, and the people watching them: accounts, alert rules, the emails
sent and the feedback given.

Rasters stay in the data store; these rows describe them. Geometries are stored in
EPSG:4326 and every area is computed in the AOI's UTM zone. An AOI around someone's home
is personal data: every AOI has one owner, and only they see it.
"""

import math
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date, datetime
from functools import cache

from geoalchemy2 import Geometry, WKBElement
from geoalchemy2.shape import to_shape
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
    UniqueConstraint,
    create_engine,
    false,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

from orbitdiff.aoi import AOIGrid, make_grid
from orbitdiff.config import settings

STAGES = ("queued", "reading", "masking", "compositing", "detecting", "ready", "failed")


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    email: Mapped[str] = mapped_column(String(254), unique=True)  # verified by a sign-in link
    created_utc: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class LoginLink(Base):
    """A sign-in link sent by email. Only a hash of its token is kept, so the link itself
    carries nothing personal and this table can't be used to sign in."""

    __tablename__ = "login_links"

    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    email: Mapped[str] = mapped_column(String(254))
    expires_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class LoginSession(Base):
    """A signed-in browser. The cookie holds the token; this row only its hash."""

    __tablename__ = "sessions"

    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    expires_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class Aoi(Base):
    __tablename__ = "aois"
    __table_args__ = (Index("ix_aois_geometry", "geometry", postgresql_using="gist"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # None only for AOIs drawn before accounts existed: no one sees them until
    # `orbitdiff assign-aois` gives them an owner.
    user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
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

    def grid(self) -> AOIGrid:
        # The size cap was enforced when the AOI was created; a later, lower cap must not
        # strand existing AOIs.
        return make_grid(str(self.id), to_shape(self.geometry), max_km2=math.inf)


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


class AlertRule(Base):
    """When changes in an AOI reach its owner by email. Evaluated only by
    `alerts.should_alert`. An AOI without a row has alerts off."""

    __tablename__ = "alert_rules"

    aoi_id: Mapped[int] = mapped_column(ForeignKey("aois.id", ondelete="CASCADE"), primary_key=True)
    enabled: Mapped[bool] = mapped_column(Boolean)
    categories: Mapped[list] = mapped_column(JSON)
    skip_seasonal: Mapped[bool] = mapped_column(Boolean)
    min_ha: Mapped[float] = mapped_column(Float)
    # Only a status set by a pass acquired after this alerts: when alerts were turned on,
    # the AOI's archive up to here was already in front of its owner.
    watching_since: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_utc: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class Notification(Base):
    """One email about one event to one user: the alert, then at most one follow-up when
    the event is confirmed, dismissed or turns out seasonal. The unique key is what keeps
    job retries from sending twice."""

    __tablename__ = "notifications"
    __table_args__ = (UniqueConstraint("event_id", "user_id", "kind"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("events.id", ondelete="CASCADE"))
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    kind: Mapped[str] = mapped_column(String(16))  # alert | update
    event_status: Mapped[str] = mapped_column(String(16))  # the status the email reported
    # claimed -> sent, or failed (retried up to alerts.send_attempts). A claim never
    # finished means the process died mid-send: it is never retried, as the email may
    # have gone out.
    state: Mapped[str] = mapped_column(String(16))
    attempts: Mapped[int] = mapped_column(Integer)
    error: Mapped[str | None] = mapped_column(String)
    claimed_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    sent_utc: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # When the pass that set the status reported was acquired: sent_utc minus this is how
    # long the alert took to reach someone (Phase 10).
    pass_utc: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class JobRun(Base):
    """One backfill or update of one AOI: what it read and how it ended. Pipeline health
    is computed from these rows (Phase 10)."""

    __tablename__ = "job_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    aoi_id: Mapped[int] = mapped_column(ForeignKey("aois.id", ondelete="CASCADE"), index=True)
    started_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    finished_utc: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(16))  # running | ok | failed
    error: Mapped[str | None] = mapped_column(String)
    passes_read: Mapped[int] = mapped_column(Integer, default=0)  # new passes in the cube
    passes_failed: Mapped[int] = mapped_column(Integer, default=0)  # left for the next run
    fallback_passes: Mapped[int] = mapped_column(Integer, default=0)  # read from the fallback
    bytes_read: Mapped[int] = mapped_column(BigInteger, default=0)
    emails_sent: Mapped[int] = mapped_column(Integer, default=0)


class Feedback(Base):
    """ "Not a real change", with the event as it was when the user said so: the precision
    signal and future training data. Biased: users only see what alerted (CLAUDE.md)."""

    __tablename__ = "feedback"
    __table_args__ = (UniqueConstraint("event_id", "user_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("events.id", ondelete="CASCADE"))
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    verdict: Mapped[str] = mapped_column(String(16))  # not_real
    via: Mapped[str] = mapped_column(String(8))  # email | app
    event: Mapped[dict] = mapped_column(JSON)  # provenance snapshot
    created_utc: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


@cache
def engine() -> Engine:
    return create_engine(settings().database_url, pool_pre_ping=True)


@contextmanager
def session() -> Iterator[Session]:
    """A session that commits on success and rolls back on error."""
    with sessionmaker(engine(), expire_on_commit=False).begin() as s:
        yield s
