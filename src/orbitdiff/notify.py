"""Alert emails: which events need one, what it says, and sending it exactly once.

An event alerts its AOI's owner once, when a pass after they turned alerts on gives it a
status their rule lets through, plus at most one follow-up when it is later confirmed,
dismissed or found seasonal. Each is claimed in the database before it is sent, so a
retried job never sends it twice. A send that fails is retried on the next run, up to
`alerts.send_attempts`. A claim that never finished (the process died while sending) is
left alone: missing an email beats sending it twice.

When more changes are due at once than anyone wants emails (`alerts.digest_over`), they
share a digest. Every change in it still has its pictures, facts and "Not a real change"
link, and is claimed on its own, so the once-only rule holds per change.
"""

import html
import re
from dataclasses import dataclass
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
from orbitdiff.config import AlertsConfig, PipelineConfig, pipeline_config, settings

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


def _batches(todo: list, cfg: AlertsConfig) -> list[list]:
    """An email per change, until so many are due at once that digests say it better."""
    if len(todo) <= cfg.digest_over:
        return [[item] for item in todo]
    return [todo[i : i + cfg.digest_size] for i in range(0, len(todo), cfg.digest_size)]


def notify(aoi_id: int) -> int:
    """Send what is due for an AOI's events; how many changes were told about."""
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
    for batch in _batches(todo, cfg.alerts):
        claims = {}  # notification id -> (event, kind)
        for event, kind in batch:
            claimed = _claim(event, user.id, kind, cfg.alerts.send_attempts)
            if claimed is not None:
                claims[claimed] = (event, kind)
        if not claims:
            continue
        try:
            mail.send(alert_email(aoi, user, list(claims.values()), cfg))
        except Exception as exc:  # recorded and retried on the next run
            for claimed in claims:
                _finish(claimed, f"{type(exc).__name__}: {exc}")
            logger.warning("notifications {} failed: {}", sorted(claims), exc)
        else:
            for claimed in claims:
                _finish(claimed, None)
            count += len(claims)
    logger.info("AOI {}: {} of {} due notifications sent", aoi_id, count, len(todo))
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
        pass_utc=event.status_utc,
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=["event_id", "user_id", "kind"],
        set_={
            "state": "claimed",
            "attempts": db.Notification.attempts + 1,
            "event_status": event.status,
            "pass_utc": event.status_utc,
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
    if kind == "alert" and event.status == "seasonal":  # the owner chose to hear about these
        return (
            f"Seasonal: {what} in {place} ({size})",
            f"A clear pass on {_day(event.history[0]['at'])} shows about {size} of {what} in "
            f"{place}, {compared}, but it looks seasonal ({_months_in(last['cause'])}).",
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


@dataclass(frozen=True)
class _Notice:
    """What an email says about one change."""

    subject: str
    lead: str
    facts: list[tuple[str, str]]
    open_link: str
    not_real: str
    images: dict[str, tuple[bytes | None, str]]  # side -> PNG, Content-ID
    months: dict[str, date]  # side -> the composite month it shows


def _notice(aoi: db.Aoi, user: db.User, event: db.Event, kind: str, cfg: PipelineConfig) -> _Notice:
    subject, lead = _words(event, kind, aoi.name)
    seen = max(1, sum(h["status"] in ("pending", "confirmed") for h in event.history))
    passes = "1 clear pass" if seen == 1 else f"{seen} clear passes"
    facts = [
        ("Size", _area(event.area_ha)),
        ("Compared", f"{_month(event.month)} with {_month(event.reference_month)}"),
        ("Status", f"{event.status}, seen on {passes}"),
        ("Method", event.method_version),
    ]
    if event.ndvi_before is not None and event.ndvi_after is not None:
        facts.insert(1, ("NDVI", f"{event.ndvi_before:.2f} → {event.ndvi_after:.2f}"))
    if event.confidence is not None:
        share = f"{event.confidence:.0%} of it changed on the clear passes that decided it"
        facts.insert(-1, ("Confidence", share))
    return _Notice(
        subject=subject,
        lead=lead,
        facts=facts,
        open_link=event_link(aoi.id, event.id),
        not_real=feedback_link(event.id, user.id, cfg),
        images={
            side: (event_image(aoi, event, side), make_msgid(domain="orbitdiff"))
            for side in ("before", "after")
        },
        months={"before": event.reference_month, "after": event.month},
    )


def _text_block(n: _Notice) -> list[str]:
    return [
        n.lead,
        "",
        *(f"{k}: {v}" for k, v in n.facts),
        "",
        f"Open in Orbit Diff: {n.open_link}",
        f"Not a real change? {n.not_real}",
    ]


def _html_block(n: _Notice, heading: bool) -> str:
    e = html.escape
    cells = "".join(
        f'<td style="padding:0 8px 0 0;vertical-align:top">'
        f'<div style="font-size:12px;color:#5c6670;margin-bottom:4px">'
        f"{side.capitalize()} · {_month(n.months[side])}</div>"
        + (
            f'<img src="cid:{cid[1:-1]}" width="192" alt="{side} {_month(n.months[side])}" '
            'style="display:block;border-radius:6px;background:#e6e9ec;max-width:100%">'
            if png
            else '<div style="width:192px;height:120px;background:#e6e9ec;border-radius:6px"></div>'
        )
        + "</td>"
        for side, (png, cid) in n.images.items()
    )
    rows = "".join(
        f'<tr><td style="padding:2px 16px 2px 0;color:#5c6670">{e(k)}</td><td>{e(v)}</td></tr>'
        for k, v in n.facts
    )
    title = f'<h2 style="margin:0 0 8px;font-size:16px">{e(n.subject)}</h2>' if heading else ""
    return f"""{title}<p style="margin:0 0 16px">{e(n.lead)}</p>
<table role="presentation" style="border-collapse:collapse;margin:0 0 16px">
<tr>{cells}</tr></table>
<table role="presentation" style="border-collapse:collapse;font-size:14px;margin:0 0 20px">
{rows}</table>
<p style="margin:0 0 20px">
<a href="{e(n.open_link)}" style="display:inline-block;background:#1c2126;color:#fff;
text-decoration:none;padding:9px 16px;border-radius:6px;margin:0 8px 8px 0">Open in Orbit Diff</a>
<a href="{e(n.not_real)}" style="display:inline-block;color:#1c2126;text-decoration:none;
padding:8px 15px;border:1px solid #c3cad1;border-radius:6px">Not a real change</a></p>"""


def alert_email(
    aoi: db.Aoi, user: db.User, changes: list[tuple[db.Event, str]], cfg: PipelineConfig
) -> EmailMessage:
    """One email about one change, or a digest about several: (event, kind) each."""
    notices = [_notice(aoi, user, event, kind, cfg) for event, kind in changes]
    digest = len(notices) > 1
    intro = (
        f"The latest passes over {aoi.name} show {len(notices)} changes. Each has its own "
        "pictures, and its own link to tell us if it isn't real."
    )
    footer = (
        f"You get this because alerts are on for {aoi.name}. Change or turn them off in Orbit Diff."
    )

    msg = EmailMessage()
    msg["Subject"] = f"{len(notices)} changes in {aoi.name}" if digest else notices[0].subject
    msg["To"] = user.email
    text = [
        line
        for n in notices
        for line in ([n.subject, ""] if digest else []) + _text_block(n) + [""]
    ]
    msg.set_content("\n".join(([intro, ""] if digest else []) + text + [footer]))

    e = html.escape
    rule = '<hr style="border:0;border-top:1px solid #e6e9ec;margin:24px 0">'
    blocks = rule.join(_html_block(n, heading=digest) for n in notices)
    opening = f'<p style="margin:0 0 20px">{e(intro)}</p>' if digest else ""
    msg.add_alternative(
        f"""<!doctype html><html><body style="margin:0;padding:24px;background:#f4f5f6;
font:15px/1.5 -apple-system,'Segoe UI',Roboto,sans-serif;color:#1c2126">
<div style="max-width:520px;margin:0 auto;background:#fff;border-radius:10px;padding:24px">
{opening}{blocks}
<p style="margin:0;font-size:12px;color:#5c6670">{e(footer)}</p>
</div></body></html>""",
        subtype="html",
    )
    body = msg.get_payload()[1]
    for notice in notices:
        for png, cid in notice.images.values():
            if png:
                body.add_related(png, "image", "png", cid=cid)
    return msg
