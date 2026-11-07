"""Is the pipeline healthy, and are its alerts still trustworthy? (Phase 10)

Plain SQL over the records the pipeline already keeps. Everything here is aggregate: AOI ids
at most, never names, geometries or email addresses.

Precision comes from "Not a real change" feedback on alerts that were sent. It is biased:
people only see what alerted, so it says nothing about the changes we missed (recall). The
frozen evaluation set (`orbitdiff evaluate`) measures both.

A category whose precision falls below `monitoring.min_precision` is flagged for review.
Nothing changes on its own: a person changes thresholds, and re-scores them on the
evaluation set first (CLAUDE.md).
"""

from datetime import datetime, timedelta

from pydantic import BaseModel
from sqlalchemy import and_, func, select
from sqlalchemy.orm import Session

from orbitdiff import buildings, db, detection
from orbitdiff.config import PipelineConfig, settings

STUCK_AFTER = timedelta(hours=1)  # a claim this old never finished: its email may be lost


class Precision(BaseModel):
    method_version: str
    category: str
    alerts: int  # alerts sent in the window
    not_real: int  # of them, marked "Not a real change"
    precision: float
    flagged: bool  # below min_precision, with enough alerts to say so


class Health(BaseModel):
    runs: int
    failed_runs: int
    passes_read: int
    passes_failed: int  # left for the next run to retry
    fallback_passes: int  # read from the fallback catalog
    bytes_read: int
    median_run_minutes: float | None
    emails_sent: int
    emails_failed: int  # given up after send_attempts, or failed and waiting to retry
    emails_stuck: int  # claimed, never finished: may or may not have gone out
    median_alert_hours: float | None  # from the pass's acquisition to the alert being sent
    feedback: int  # "Not a real change", on any change


class Failure(BaseModel):
    aoi_id: int
    finished_utc: datetime
    error: str


class Summary(BaseModel):
    production_version: str  # the method new events come from
    precision_days: int
    health_days: int
    min_precision: float
    min_alerts: int  # fewer alerts than this in the window: too few to flag
    precision: list[Precision]
    health: Health
    failures: list[Failure]


def _precision(s: Session, since: datetime, cfg: PipelineConfig) -> list[Precision]:
    feedback = and_(
        db.Feedback.event_id == db.Notification.event_id,
        db.Feedback.user_id == db.Notification.user_id,
        db.Feedback.verdict == "not_real",
    )
    rows = s.execute(
        select(
            db.Event.method_version,
            db.Event.category,
            func.count(),
            func.count(db.Feedback.id),
        )
        .select_from(db.Notification)
        .join(db.Event, db.Event.id == db.Notification.event_id)
        .outerjoin(db.Feedback, feedback)
        .where(
            db.Notification.kind == "alert",
            db.Notification.state == "sent",
            db.Notification.sent_utc >= since,
        )
        .group_by(db.Event.method_version, db.Event.category)
        .order_by(db.Event.method_version, db.Event.category)
    ).all()
    m = cfg.monitoring
    out = []
    for version, category, alerts, not_real in rows:
        precision = (alerts - not_real) / alerts
        out.append(
            Precision(
                method_version=version,
                category=category,
                alerts=alerts,
                not_real=not_real,
                precision=round(precision, 3),
                flagged=alerts >= m.min_alerts and precision < m.min_precision,
            )
        )
    return out


def _median(column) -> object:
    return func.percentile_cont(0.5).within_group(column)


def _health(s: Session, since: datetime, now: datetime) -> Health:
    runs = s.execute(
        select(
            func.count(),
            func.count().filter(db.JobRun.status == "failed"),
            func.coalesce(func.sum(db.JobRun.passes_read), 0),
            func.coalesce(func.sum(db.JobRun.passes_failed), 0),
            func.coalesce(func.sum(db.JobRun.fallback_passes), 0),
            func.coalesce(func.sum(db.JobRun.bytes_read), 0),
            _median(
                func.extract("epoch", db.JobRun.finished_utc - db.JobRun.started_utc) / 60
            ).filter(db.JobRun.finished_utc.is_not(None)),
        ).where(db.JobRun.started_utc >= since)
    ).one()
    sent = and_(db.Notification.state == "sent", db.Notification.sent_utc >= since)
    emails = s.execute(
        select(
            func.count().filter(sent),
            func.count().filter(db.Notification.state == "failed"),
            func.count().filter(
                db.Notification.state == "claimed",
                db.Notification.claimed_utc < now - STUCK_AFTER,
            ),
            _median(
                func.extract("epoch", db.Notification.sent_utc - db.Notification.pass_utc) / 3600
            ).filter(sent, db.Notification.kind == "alert", db.Notification.pass_utc.is_not(None)),
        ).where(db.Notification.claimed_utc >= since)
    ).one()
    feedback = s.scalar(
        select(func.count()).where(
            db.Feedback.created_utc >= since, db.Feedback.verdict == "not_real"
        )
    )
    return Health(
        runs=runs[0],
        failed_runs=runs[1],
        passes_read=runs[2],
        passes_failed=runs[3],
        fallback_passes=runs[4],
        bytes_read=runs[5],
        median_run_minutes=None if runs[6] is None else round(float(runs[6]), 1),
        emails_sent=emails[0],
        emails_failed=emails[1],
        emails_stuck=emails[2],
        median_alert_hours=None if emails[3] is None else round(float(emails[3]), 1),
        feedback=feedback,
    )


def summary(s: Session, cfg: PipelineConfig, now: datetime) -> Summary:
    m = cfg.monitoring
    failures = s.execute(
        select(db.JobRun.aoi_id, db.JobRun.finished_utc, db.JobRun.error)
        .where(
            db.JobRun.status == "failed",
            db.JobRun.started_utc >= now - timedelta(days=m.health_days),
        )
        .order_by(db.JobRun.finished_utc.desc())
        .limit(m.failures_shown)
    ).all()
    champion = buildings.champion_named(settings().data_dir)
    model = champion and detection.Learned(champion, cfg.buildings.threshold)
    method = detection.methods(cfg.detection, model)[detection.PRODUCTION]
    return Summary(
        production_version=detection.version(method, cfg.detection),
        precision_days=m.precision_days,
        health_days=m.health_days,
        min_precision=m.min_precision,
        min_alerts=m.min_alerts,
        precision=_precision(s, now - timedelta(days=m.precision_days), cfg),
        health=_health(s, now - timedelta(days=m.health_days), now),
        failures=[Failure(aoi_id=a, finished_utc=f, error=e or "") for a, f, e in failures],
    )
