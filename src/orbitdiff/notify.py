"""Alert emails: which events need one, what it says, and sending it exactly once.

An event alerts its AOI's owner once, when a pass after they turned alerts on gives it a
status their rule lets through, plus at most one follow-up when it is later confirmed,
dismissed or found seasonal. Each email is claimed in the database before it is sent, so
a retried job never sends it twice. A send that fails is retried on the next run, up to
`alerts.send_attempts`. A claim that never finished (the process died while sending) is
left alone: missing an email beats sending it twice.
"""

import html
import re
from datetime import UTC, date, datetime, timedelta
from email.message import EmailMessage
from email.utils import make_msgid

from geoalchemy2.shape import to_shape
from loguru import logger
from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert

from orbitdiff import accounts, alerts, composites, db, mail, render
from orbitdiff.alerts import AlertRule
from orbitdiff.aoi import reproject
from orbitdiff.config import PipelineConfig, pipeline_config, settings

FOLLOW_UP = frozenset({"confirmed", "dismissed", "seasonal"})
WHAT = {
    "buildings_roads": "new buildings or roads",
    "vegetation_loss": "vegetation loss",
    "solar_industry": "new solar panels or industry",
    "water": "a change in water",
    "other": "a change",
}


def due(
    event: db.Event, rule: AlertRule, watching_since: datetime, sent: dict[str, db.Notification]
) -> str | None:
    """The email `event` needs now, if any: "alert" or "update". `sent` holds the
    notifications already recorded for it, by kind."""
    if not rule.enabled:
        return None
    alert = sent.get("alert")
    if alert is None or alert.state == "failed":
        ok, _ = alerts.should_alert(event.status, event.category, event.area_ha, rule)
        return "alert" if ok and event.status_utc > watching_since else None
    followed = sent.get("update")
    if (
        alert.state == "sent"
        and (followed is None or followed.state == "failed")
        and event.status != alert.event_status
        and event.status in FOLLOW_UP
    ):
        return "update"
    return None


def notify(aoi_id: int) -> int:
    """Send the emails due for an AOI's events; how many went out."""
    cfg = pipeline_config()
    with db.session() as s:
        aoi = s.get(db.Aoi, aoi_id)
        row = s.get(db.AlertRule, aoi_id)
        if aoi is None or aoi.user_id is None or row is None or row.watching_since is None:
            return 0
        rule, user = alerts.rule_of(row, cfg.alerts), s.get(db.User, aoi.user_id)
        sent: dict[int, dict[str, db.Notification]] = {}
        for n in s.scalars(
            select(db.Notification)
            .join(db.Event)
            .where(db.Event.aoi_id == aoi_id, db.Notification.user_id == user.id)
        ):
            sent.setdefault(n.event_id, {})[n.kind] = n
        events = s.scalars(
            select(db.Event).where(db.Event.aoi_id == aoi_id).order_by(db.Event.detected_utc)
        )
        todo = [
            (e, kind)
            for e in events
            if (kind := due(e, rule, row.watching_since, sent.get(e.id, {})))
        ]
    count = 0
    for event, kind in todo:
        claimed = _claim(event, user.id, kind, cfg.alerts.send_attempts)
        if claimed is None:
            continue
        try:
            mail.send(alert_email(aoi, user, event, kind, cfg))
        except Exception as exc:  # recorded and retried on the next run
            _finish(claimed, f"{type(exc).__name__}: {exc}")
            logger.warning(
                "notification {} ({} for event {}) failed: {}", claimed, kind, event.id, exc
            )
        else:
            _finish(claimed, None)
            count += 1
    logger.info("AOI {}: {} of {} due emails sent", aoi_id, count, len(todo))
    return count


def _claim(event: db.Event, user_id: int, kind: str, max_attempts: int) -> int | None:
    """Record that this email is being sent, committed before sending. None when it was
    already sent, is being sent, or has failed too often."""
    now = datetime.now(UTC)
    stmt = insert(db.Notification).values(
        event_id=event.id,
        user_id=user_id,
        kind=kind,
        event_status=event.status,
        state="claimed",
        attempts=1,
        claimed_utc=now,
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=["event_id", "user_id", "kind"],
        set_={
            "state": "claimed",
            "attempts": db.Notification.attempts + 1,
            "event_status": event.status,
            "claimed_utc": now,
            "error": None,
        },
        where=(db.Notification.state == "failed") & (db.Notification.attempts < max_attempts),
    ).returning(db.Notification.id)
    with db.session() as s:
        return s.execute(stmt).scalar_one_or_none()


def _finish(notification_id: int, error: str | None) -> None:
    values = (
        {"state": "failed", "error": error[:500]}
        if error
        else {"state": "sent", "sent_utc": datetime.now(UTC)}
    )
    with db.session() as s:
        s.execute(
            update(db.Notification).where(db.Notification.id == notification_id).values(values)
        )


def event_image(aoi: db.Aoi, event: db.Event, side: str) -> bytes | None:
    """PNG crop around an event, outlined, from the composite on one side of the comparison
    ("before" or "after"); None when that composite is gone. Unobserved pixels stay
    transparent: nothing is filled in. The app and the emails show the same picture."""
    month = event.reference_month if side == "before" else event.month
    path = composites.composite_dir(settings().data_dir, str(aoi.id)) / f"{month:%Y-%m}.tif"
    if not path.exists():
        return None
    grid = aoi.grid()
    area = reproject(to_shape(event.geometry), 4326, grid.epsg)
    return render.png(render.crop(composites.read_composite(path), grid, area))


def event_link(aoi_id: int, event_id: int) -> str:
    return f"{settings().web_url}/#/areas/{aoi_id}/changes/{event_id}"


def feedback_link(event_id: int, user_id: int, cfg: PipelineConfig) -> str:
    """A "Not a real change" link that works without signing in, for this event and user."""
    days = cfg.alerts.feedback_link_days
    token = accounts.sign("feedback", {"e": event_id, "u": user_id}, timedelta(days=days))
    return f"{settings().api_url}/feedback/{token}"


def _day(at: str | datetime) -> str:
    at = datetime.fromisoformat(at) if isinstance(at, str) else at
    return f"{at.day} {at:%B %Y}"  # acquisition dates are UTC dates


def _month(d: date) -> str:
    return f"{d:%B %Y}"


def _months_in(text: str) -> str:
    """ "reverted by 2026-08" -> "reverted by August 2026"."""
    return re.sub(r"\b(\d{4})-(\d{2})\b", lambda m: _month(date(int(m[1]), int(m[2]), 1)), text)


def _area(ha: float) -> str:
    return f"{ha:.1f} ha" if ha < 10 else f"{ha:.0f} ha"


def _words(event: db.Event, kind: str, place: str) -> tuple[str, str]:
    """Subject and lead paragraph."""
    what, size = WHAT[event.category], _area(event.area_ha)
    last = event.history[-1]
    compared = f"compared with {_month(event.reference_month)}"
    if kind == "alert" and event.status == "pending":
        return (
            f"Possible {what} in {place} ({size})",
            f"A clear pass on {_day(event.history[0]['at'])} shows about {size} of {what} in "
            f"{place}, {compared}. One pass is a suspicion, not a change yet: we'll write "
            "again when the next clear pass confirms or dismisses it.",
        )
    if kind == "alert":
        return (
            f"{what.capitalize()} in {place} ({size}), confirmed",
            f"Clear passes on {_day(event.history[0]['at'])} and {_day(last['at'])} both show "
            f"about {size} of {what} in {place}, {compared}.",
        )
    if event.status == "confirmed":
        return (
            f"Confirmed: {what} in {place} ({size})",
            f"The clear pass on {_day(last['at'])} shows it again, so the {what} in {place} "
            "is confirmed.",
        )
    if event.status == "dismissed":
        reason = (
            "After a change to how Orbit Diff finds changes, it's no longer found"
            if last["cause"] == "no longer detected"
            else f"The next clear pass, on {_day(last['at'])}, doesn't show it"
        )
        return (
            f"Not confirmed: {what} in {place}",
            f"{reason}, so we've dismissed it. Sorry for the false alarm.",
        )
    return (
        f"Seasonal: {what} in {place}",
        f"It has turned out to be seasonal ({_months_in(last['cause'])}), so it no longer "
        "counts as a change.",
    )


def alert_email(
    aoi: db.Aoi, user: db.User, event: db.Event, kind: str, cfg: PipelineConfig
) -> EmailMessage:
    subject, lead = _words(event, kind, aoi.name)
    passes = "1 clear pass" if event.status == "pending" else "2 clear passes"
    facts = [
        ("Size", _area(event.area_ha)),
        ("Compared", f"{_month(event.month)} with {_month(event.reference_month)}"),
        ("Status", f"{event.status}, seen on {passes}"),
        ("Method", event.method_version),
    ]
    if event.ndvi_before is not None and event.ndvi_after is not None:
        facts.insert(1, ("NDVI", f"{event.ndvi_before:.2f} → {event.ndvi_after:.2f}"))
    open_link = event_link(aoi.id, event.id)
    not_real = feedback_link(event.id, user.id, cfg)
    footer = (
        f"You get this because alerts are on for {aoi.name}. Change or turn them off in Orbit Diff."
    )

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["To"] = user.email
    msg.set_content(
        "\n".join(
            [
                lead,
                "",
                *(f"{k}: {v}" for k, v in facts),
                "",
                f"Open in Orbit Diff: {open_link}",
                f"Not a real change? {not_real}",
                "",
                footer,
            ]
        )
    )
    images = {
        side: (event_image(aoi, event, side), make_msgid(domain="orbitdiff"))
        for side in ("before", "after")
    }
    months = {"before": event.reference_month, "after": event.month}
    e = html.escape
    cells = "".join(
        f'<td style="padding:0 8px 0 0;vertical-align:top">'
        f'<div style="font-size:12px;color:#5c6670;margin-bottom:4px">'
        f"{side.capitalize()} · {_month(months[side])}</div>"
        + (
            f'<img src="cid:{cid[1:-1]}" width="192" alt="{side} {_month(months[side])}" '
            'style="display:block;border-radius:6px;background:#e6e9ec;max-width:100%">'
            if png
            else '<div style="width:192px;height:120px;background:#e6e9ec;border-radius:6px"></div>'
        )
        + "</td>"
        for side, (png, cid) in images.items()
    )
    rows = "".join(
        f'<tr><td style="padding:2px 16px 2px 0;color:#5c6670">{e(k)}</td><td>{e(v)}</td></tr>'
        for k, v in facts
    )
    msg.add_alternative(
        f"""<!doctype html><html><body style="margin:0;padding:24px;background:#f4f5f6;
font:15px/1.5 -apple-system,'Segoe UI',Roboto,sans-serif;color:#1c2126">
<div style="max-width:520px;margin:0 auto;background:#fff;border-radius:10px;padding:24px">
<p style="margin:0 0 16px">{e(lead)}</p>
<table role="presentation" style="border-collapse:collapse;margin:0 0 16px">
<tr>{cells}</tr></table>
<table role="presentation" style="border-collapse:collapse;font-size:14px;margin:0 0 20px">
{rows}</table>
<p style="margin:0 0 20px">
<a href="{e(open_link)}" style="display:inline-block;background:#1c2126;color:#fff;
text-decoration:none;padding:9px 16px;border-radius:6px;margin:0 8px 8px 0">Open in Orbit Diff</a>
<a href="{e(not_real)}" style="display:inline-block;color:#1c2126;text-decoration:none;
padding:8px 15px;border:1px solid #c3cad1;border-radius:6px">Not a real change</a></p>
<p style="margin:0;font-size:12px;color:#5c6670">{e(footer)}</p>
</div></body></html>""",
        subtype="html",
    )
    body = msg.get_payload()[1]
    for png, cid in images.values():
        if png:
            body.add_related(png, "image", "png", cid=cid)
    return msg
