"""The REST API the web app talks to. Run: `uv run uvicorn orbitdiff.api:app`.

Every AOI endpoint needs a signed-in user and answers only for that user's own AOIs: any
other AOI is a 404, never a 403, so ids don't reveal what exists. The map tiles TiTiler
serves are not behind this yet (Phase 11), so this must not be exposed beyond a
development machine.
"""

import base64
import html
from collections.abc import Callable, Iterator
from datetime import UTC, date, datetime
from typing import Annotated, Literal

import numpy as np
from fastapi import Cookie, Depends, FastAPI, HTTPException, Query, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, RedirectResponse
from geoalchemy2.shape import from_shape, to_shape
from loguru import logger
from pydantic import BaseModel, Field
from redis import Redis
from shapely.geometry import mapping, shape
from sqlalchemy import func, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from orbitdiff import accounts, alerts, cube, db, jobs, mail, monitoring, notify, render, tiles
from orbitdiff.aoi import make_grid, reproject
from orbitdiff.config import pipeline_config, settings
from orbitdiff.detection import CATEGORIES

app = FastAPI(title="Orbit Diff")
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings().cors_origins,
    allow_credentials=True,  # the session cookie
    allow_methods=["*"],
    allow_headers=["*"],
)


def get_session() -> Iterator[Session]:
    with Session(db.engine(), expire_on_commit=False) as s:  # endpoints commit their writes
        yield s


def get_enqueue() -> Callable[[int], None]:
    return jobs.enqueue_backfill


def get_redis() -> Redis:
    return Redis.from_url(settings().redis_url, socket_timeout=2)


SessionDep = Annotated[Session, Depends(get_session)]
SessionCookie = Annotated[str | None, Cookie(alias=accounts.COOKIE)]


def current_user(s: SessionDep, token: SessionCookie = None) -> db.User:
    user = accounts.user_for_session(s, token) if token else None
    if user is None:
        raise HTTPException(401, "sign in first")
    return user


UserDep = Annotated[db.User, Depends(current_user)]


def _is_admin(user: db.User) -> bool:
    return user.email in {e.strip().lower() for e in settings().admin_emails}


class Polygon(BaseModel):
    type: Literal["Polygon"]
    coordinates: list[list[tuple[float, float]]]


class AoiCreate(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    geometry: Polygon


class AoiSummary(BaseModel):
    id: int
    name: str
    area_ha: float
    stage: str
    alerts_on: bool


class AoiDetail(AoiSummary):
    geometry: dict
    corners: list[tuple[float, float]]  # lon/lat of the AOI grid: TL, TR, BR, BL (scene images)
    archive_start: date
    first_month: date | None
    last_month: date | None
    scenes: int  # passes indexed
    clear_scenes: int  # passes with at least scene_min_clear_frac of the AOI clear
    bytes_read: int  # what reading the AOI's windows cost: shown to users
    whole_files_bytes: int  # estimated cost of downloading every pass's files whole


class Progress(BaseModel):
    stage: str
    done: int
    total: int
    error: str | None


class MonthOut(BaseModel):
    month: date
    passes: int
    with_bands: int
    clear_scenes: int
    observed_frac: float
    filled_frac: float
    gap: bool  # no clear observation anywhere in the AOI: the display is all filled
    tiles: dict[str, str]


class NdviPoint(BaseModel):
    month: date
    ndvi: float | None  # mean over the AOI's observed pixels
    observed_frac: float


class SceneOut(BaseModel):
    item_id: str
    acquired_utc: datetime
    platform: str
    catalog: str
    read_status: str
    aoi_clear_frac: float | None
    offset_suspect: bool  # its offset flag contradicts its pixels: left out of composites
    images: dict[str, str] | None  # only for passes whose bands were read


class Limits(BaseModel):
    max_area_km2: float  # the AOI size cap, shown while drawing


@app.get("/limits")
def limits() -> Limits:
    return Limits(max_area_km2=pipeline_config().aoi.max_km2)


def _aoi(s: Session, aoi_id: int, user: db.User) -> db.Aoi:
    aoi = s.get(db.Aoi, aoi_id)
    if aoi is None or aoi.user_id != user.id:
        raise HTTPException(404, "no such AOI")
    return aoi


def _summary(aoi: db.Aoi, alerts_on: bool) -> AoiSummary:
    return AoiSummary(
        id=aoi.id,
        name=aoi.name,
        area_ha=round(aoi.area_ha, 2),
        stage=aoi.stage,
        alerts_on=alerts_on,
    )


def _alerts_on(s: Session, aoi_id: int) -> bool:
    rule = s.get(db.AlertRule, aoi_id)
    return rule is not None and rule.enabled


@app.post("/aois", status_code=201)
def create_aoi(
    body: AoiCreate,
    s: SessionDep,
    user: UserDep,
    enqueue: Annotated[Callable, Depends(get_enqueue)],
) -> AoiSummary:
    cfg = pipeline_config()
    try:
        grid = make_grid("new", shape(body.geometry.model_dump()), cfg.aoi.max_km2)
    except ValueError as exc:  # not a valid polygon, or over the size cap
        raise HTTPException(422, str(exc)) from exc
    aoi = db.Aoi(
        user_id=user.id,
        name=body.name,
        geometry=from_shape(grid.geometry, srid=4326),
        epsg=grid.epsg,
        area_ha=grid.area_km2 * 100,
        archive_start=cfg.aoi.archive_start,
        stage="queued",
        progress_done=0,
        progress_total=0,
    )
    s.add(aoi)
    s.commit()
    enqueue(aoi.id)
    return _summary(aoi, alerts_on=False)


@app.get("/aois")
def list_aois(s: SessionDep, user: UserDep) -> list[AoiSummary]:
    watched = set(s.scalars(select(db.AlertRule.aoi_id).where(db.AlertRule.enabled)))
    rows = s.scalars(select(db.Aoi).where(db.Aoi.user_id == user.id).order_by(db.Aoi.id))
    return [_summary(a, a.id in watched) for a in rows]


@app.get("/aois/{aoi_id}")
def get_aoi(aoi_id: int, s: SessionDep, user: UserDep) -> AoiDetail:
    aoi = _aoi(s, aoi_id, user)
    grid = aoi.grid()
    left, bottom, right, top = grid.bounds
    corners = reproject(
        shape(
            {
                "type": "MultiPoint",
                "coordinates": [(left, top), (right, top), (right, bottom), (left, bottom)],
            }
        ),
        grid.epsg,
        4326,
    )
    min_clear = pipeline_config().masking.scene_min_clear_frac
    scenes = s.execute(
        select(
            func.count(),
            func.count().filter(db.Scene.aoi_clear_frac >= min_clear),
            func.coalesce(func.sum(db.Scene.bytes_read), 0),
            func.avg(db.Scene.full_bytes).filter(db.Scene.read_status == "full"),
        ).where(db.Scene.aoi_id == aoi_id)
    ).one()
    months = s.execute(
        select(func.min(db.Month.month), func.max(db.Month.month)).where(db.Month.aoi_id == aoi_id)
    ).one()
    return AoiDetail(
        **_summary(aoi, _alerts_on(s, aoi_id)).model_dump(),
        geometry=mapping(to_shape(aoi.geometry)),
        corners=[(round(p.x, 7), round(p.y, 7)) for p in corners.geoms],
        archive_start=aoi.archive_start,
        first_month=months[0],
        last_month=months[1],
        scenes=scenes[0],
        clear_scenes=scenes[1],
        bytes_read=scenes[2],
        whole_files_bytes=round((scenes[3] or 0) * scenes[0]),
    )


@app.post("/aois/{aoi_id}/backfill", status_code=202)
def rerun_backfill(
    aoi_id: int,
    s: SessionDep,
    user: UserDep,
    enqueue: Annotated[Callable, Depends(get_enqueue)],
) -> Progress:
    """Retry a failed backfill, or pick up passes since the last one."""
    aoi = _aoi(s, aoi_id, user)
    if not jobs.claim_backfill(s, aoi_id):
        raise HTTPException(409, f"backfill already {aoi.stage}")
    s.commit()
    enqueue(aoi_id)
    return Progress(stage="queued", done=0, total=0, error=None)


@app.get("/aois/{aoi_id}/progress")
def progress(aoi_id: int, s: SessionDep, user: UserDep) -> Progress:
    aoi = _aoi(s, aoi_id, user)
    return Progress(
        stage=aoi.stage, done=aoi.progress_done, total=aoi.progress_total, error=aoi.error
    )


def _months(s: Session, aoi_id: int, user: db.User) -> list[db.Month]:
    _aoi(s, aoi_id, user)
    return list(
        s.scalars(select(db.Month).where(db.Month.aoi_id == aoi_id).order_by(db.Month.month))
    )


@app.get("/aois/{aoi_id}/timeline")
def timeline(aoi_id: int, s: SessionDep, user: UserDep) -> list[MonthOut]:
    return [
        MonthOut(
            month=m.month,
            passes=m.passes,
            with_bands=m.with_bands,
            clear_scenes=m.clear_scenes,
            observed_frac=m.observed_frac,
            filled_frac=m.filled_frac,
            gap=m.observed_frac == 0,
            tiles=tiles.composite_urls(aoi_id, m.month, m.version),
        )
        for m in _months(s, aoi_id, user)
    ]


@app.get("/aois/{aoi_id}/ndvi")
def ndvi_series(aoi_id: int, s: SessionDep, user: UserDep) -> list[NdviPoint]:
    return [
        NdviPoint(month=m.month, ndvi=m.ndvi_mean, observed_frac=m.observed_frac)
        for m in _months(s, aoi_id, user)
    ]


@app.get("/aois/{aoi_id}/scenes")
def scenes(
    aoi_id: int,
    s: SessionDep,
    user: UserDep,
    month: Annotated[str, Query(pattern=r"^\d{4}-\d{2}$", description="YYYY-MM, UTC")],
) -> list[SceneOut]:
    _aoi(s, aoi_id, user)
    start = datetime.strptime(month, "%Y-%m").replace(tzinfo=UTC)
    end = start.replace(year=start.year + start.month // 12, month=start.month % 12 + 1)
    rows = s.scalars(
        select(db.Scene)
        .where(
            db.Scene.aoi_id == aoi_id,
            db.Scene.acquired_utc >= start,
            db.Scene.acquired_utc < end,
        )
        .order_by(db.Scene.acquired_utc)
    )
    return [
        SceneOut(
            item_id=r.item_id,
            acquired_utc=r.acquired_utc,
            platform=r.platform,
            catalog=r.catalog,
            read_status=r.read_status,
            aoi_clear_frac=r.aoi_clear_frac,
            offset_suspect=r.offset_suspect,
            images={
                style: f"/aois/{aoi_id}/scenes/{r.item_id}/{style}.png"
                for style in ("true_color", "ndvi")
            }
            if r.read_status == "full"
            else None,
        )
        for r in rows
    ]


@app.get("/aois/{aoi_id}/scenes/{item_id}/{style}.png")
def scene_image(
    aoi_id: int, item_id: str, style: Literal["true_color", "ndvi"], s: SessionDep, user: UserDep
) -> Response:
    """One pass over the AOI grid as the pipeline saw it, harmonized, from the cube.
    Place it on the map with the AOI's `corners`."""
    _aoi(s, aoi_id, user)
    ds = cube.load(cube.cube_path(settings().data_dir, str(aoi_id)))
    index = np.flatnonzero(ds["item_id"].values == item_id) if ds is not None else []
    if len(index) != 1 or ds["status"].values[index[0]] != "full":
        raise HTTPException(404, "no image for this pass")
    refl = cube.reflectance(ds.isel(time=index))
    red, green, blue, nir = (refl[b].values[0] for b in ("red", "green", "blue", "nir"))
    has_data = ~np.isnan(red)
    if style == "true_color":
        rgb = render.true_color(red, green, blue)
    else:
        rgb = render.ndvi_rgb(render.ndvi(red, nir))
    return Response(
        render.png(render.with_alpha(rgb, has_data)),
        media_type="image/png",
        headers={"Cache-Control": "private, max-age=31536000, immutable"},  # a pass never changes
    )


class StatusChange(BaseModel):
    status: str
    cause: str  # the pass (STAC item id) or month that caused it
    at: datetime


class EventOut(BaseModel):
    id: int
    month: date  # the composite month it first showed in
    reference_month: date  # compared with: the same month a year earlier
    category: str
    status: str
    area_ha: float
    geometry: dict  # GeoJSON, EPSG:4326
    ndvi_before: float | None
    ndvi_after: float | None
    detected_utc: datetime
    status_utc: datetime
    history: list[StatusChange]
    items: list[str]
    method_version: str
    alerts: bool  # whether it would have alerted at any point, under the area's rule settings
    alert_reason: str  # why not, when it wouldn't: "seasonal", "category off", ...
    not_real: bool  # the user said it isn't a real change


def _feedback_given(s: Session, user: db.User, event_ids: list[int]) -> set[int]:
    return set(
        s.scalars(
            select(db.Feedback.event_id).where(
                db.Feedback.user_id == user.id, db.Feedback.event_id.in_(event_ids)
            )
        )
    )


@app.get("/aois/{aoi_id}/events")
def events(aoi_id: int, s: SessionDep, user: UserDep) -> list[EventOut]:
    """The AOI's changes, newest first, with their provenance and whether each would have
    alerted under the area's rule settings (whether or not alerts are switched on)."""
    _aoi(s, aoi_id, user)
    rule = _rule(s, aoi_id)
    rows = list(
        s.scalars(
            select(db.Event).where(db.Event.aoi_id == aoi_id).order_by(db.Event.detected_utc.desc())
        )
    )
    given = _feedback_given(s, user, [e.id for e in rows])
    out = []
    for e in rows:
        ok, why = _would_alert(e, rule)
        fields = {c: getattr(e, c) for c in EventOut.model_fields if hasattr(db.Event, c)}
        out.append(
            EventOut(
                **fields | {"geometry": mapping(to_shape(e.geometry))},
                alerts=ok,
                alert_reason=why,
                not_real=e.id in given,
            )
        )
    return out


def _event(s: Session, aoi_id: int, event_id: int) -> db.Event:
    event = s.get(db.Event, event_id)
    if event is None or event.aoi_id != aoi_id:
        raise HTTPException(404, "no such event")
    return event


@app.get("/aois/{aoi_id}/events/{event_id}/{side}.png")
def event_image(
    aoi_id: int, event_id: int, side: Literal["before", "after"], s: SessionDep, user: UserDep
) -> Response:
    """True colour around an event from the composite on that side of the comparison, its
    footprint outlined. Pixels no clear pass saw stay transparent."""
    aoi = _aoi(s, aoi_id, user)
    image = notify.event_image(aoi, _event(s, aoi_id, event_id), side)
    if image is None:
        raise HTTPException(404, "no composite for that month")
    # A composite is recomputed when its masks or settings change, so no immutable here.
    return Response(
        image, media_type="image/png", headers={"Cache-Control": "private, max-age=3600"}
    )


def _provenance(event: db.Event) -> dict:
    """Everything that explains an event, as JSON: what feedback is stored with."""
    return {
        "event_id": event.id,
        "aoi_id": event.aoi_id,
        "month": event.month.isoformat(),
        "reference_month": event.reference_month.isoformat(),
        "category": event.category,
        "status": event.status,
        "area_ha": event.area_ha,
        "geometry": mapping(to_shape(event.geometry)),
        "ndvi_before": event.ndvi_before,
        "ndvi_after": event.ndvi_after,
        "detected_utc": event.detected_utc.isoformat(),
        "history": event.history,
        "items": event.items,
        "method_version": event.method_version,
        "thresholds": event.thresholds,
    }


def _record_not_real(s: Session, event: db.Event, user_id: int, via: str) -> None:
    """Store "not a real change" once per event and user; saying it again changes nothing."""
    s.execute(
        insert(db.Feedback)
        .values(
            event_id=event.id,
            user_id=user_id,
            verdict="not_real",
            via=via,
            event=_provenance(event),
        )
        .on_conflict_do_nothing()
    )
    s.commit()
    logger.info("feedback: event {} not a real change ({}, via {})", event.id, event.category, via)


@app.post("/aois/{aoi_id}/events/{event_id}/feedback", status_code=204)
def not_real(aoi_id: int, event_id: int, s: SessionDep, user: UserDep) -> None:
    _aoi(s, aoi_id, user)
    _record_not_real(s, _event(s, aoi_id, event_id), user.id, via="app")


class AlertRuleIn(BaseModel):
    enabled: bool
    categories: list[str]
    skip_seasonal: bool
    min_ha: float


class AlertRuleOut(AlertRuleIn):
    watching_since: datetime | None  # only passes acquired after this alert
    min_ha_range: tuple[float, float]  # the "smallest change" slider


class RulePreview(BaseModel):
    changes: int  # past changes in this AOI
    would_alert: int  # of them, those these settings would have sent an alert for
    retracted: int  # of those, the ones later dismissed or found seasonal: false alarms


def _rule(s: Session, aoi_id: int) -> alerts.AlertRule:
    return alerts.rule_of(s.get(db.AlertRule, aoi_id), pipeline_config().alerts)


def _min_ha_range() -> tuple[float, float]:
    cfg = pipeline_config()
    return cfg.detection.min_mapping_unit_ha, cfg.alerts.max_min_ha


def _would_alert(event: db.Event, rule: alerts.AlertRule) -> tuple[bool, str]:
    """Under the rule's settings, switched on: the list and the preview say what these
    settings let through, whether or not alerts are on right now."""
    statuses = [h["status"] for h in event.history]
    on = alerts.AlertRule(
        min_ha=rule.min_ha, categories=rule.categories, skip_seasonal=rule.skip_seasonal
    )
    return alerts.would_alert(statuses, event.category, event.area_ha, on)


def _checked(body: AlertRuleIn) -> alerts.AlertRule:
    low, high = _min_ha_range()
    if not low <= body.min_ha <= high:
        raise HTTPException(422, f"smallest change must be between {low:g} and {high:g} ha")
    if unknown := set(body.categories) - set(CATEGORIES):
        raise HTTPException(422, f"unknown categories: {sorted(unknown)}")
    return alerts.AlertRule(
        min_ha=body.min_ha,
        enabled=body.enabled,
        categories=frozenset(body.categories),
        skip_seasonal=body.skip_seasonal,
    )


def _rule_out(rule: alerts.AlertRule, watching_since: datetime | None) -> AlertRuleOut:
    return AlertRuleOut(
        enabled=rule.enabled,
        categories=[c for c in CATEGORIES if c in rule.categories],
        skip_seasonal=rule.skip_seasonal,
        min_ha=rule.min_ha,
        watching_since=watching_since,
        min_ha_range=_min_ha_range(),
    )


@app.get("/aois/{aoi_id}/alert-rule")
def get_alert_rule(aoi_id: int, s: SessionDep, user: UserDep) -> AlertRuleOut:
    _aoi(s, aoi_id, user)
    row = s.get(db.AlertRule, aoi_id)
    return _rule_out(_rule(s, aoi_id), row.watching_since if row else None)


@app.put("/aois/{aoi_id}/alert-rule")
def put_alert_rule(aoi_id: int, body: AlertRuleIn, s: SessionDep, user: UserDep) -> AlertRuleOut:
    """Save the AOI's rule. Turning alerts on starts watching from the newest pass already
    in the archive, so the archive's own history never arrives as a pile of emails."""
    aoi = _aoi(s, aoi_id, user)
    rule = _checked(body)
    row = s.get(db.AlertRule, aoi_id)
    since = row.watching_since if row is not None and row.enabled else None
    if rule.enabled and since is None:
        newest = s.scalar(select(func.max(db.Scene.acquired_utc)).where(db.Scene.aoi_id == aoi_id))
        # While a backfill runs the archive isn't complete: start from now instead.
        since = newest if aoi.stage == "ready" and newest else datetime.now(UTC)
    values = {
        "enabled": rule.enabled,
        "categories": sorted(rule.categories),
        "skip_seasonal": rule.skip_seasonal,
        "min_ha": rule.min_ha,
        "watching_since": since if rule.enabled else None,
    }
    if row is None:
        s.add(db.AlertRule(aoi_id=aoi_id, **values))
    else:
        for name, value in values.items():
            setattr(row, name, value)
    s.commit()
    return _rule_out(rule, values["watching_since"])


@app.post("/aois/{aoi_id}/alert-rule/preview")
def preview_alert_rule(aoi_id: int, body: AlertRuleIn, s: SessionDep, user: UserDep) -> RulePreview:
    """ "N of M past changes would have alerted", for settings not saved yet."""
    _aoi(s, aoi_id, user)
    rule = _checked(body)
    rows = list(s.scalars(select(db.Event).where(db.Event.aoi_id == aoi_id)))
    alerted = [e for e in rows if _would_alert(e, rule)[0]]
    return RulePreview(
        changes=len(rows),
        would_alert=len(alerted),
        retracted=sum(e.status in ("dismissed", "seasonal") for e in alerted),
    )


class Login(BaseModel):
    email: str = Field(max_length=accounts.MAX_EMAIL)


class Me(BaseModel):
    email: str
    admin: bool  # sees the admin view


class LinkSent(BaseModel):
    minutes: int  # how long the link works


@app.post("/auth/login", status_code=202)
def login(body: Login, s: SessionDep) -> LinkSent:
    """Email a sign-in link. The answer is the same whether or not the address has an
    account, so it can't be used to find out who does."""
    email = accounts.normalize_email(body.email)
    if email is None:
        raise HTTPException(422, "that doesn't look like an email address")
    cfg = pipeline_config().accounts
    token = accounts.new_login_link(s, email, cfg)
    s.commit()
    link = f"{settings().api_url}/auth/callback?token={token}"
    mail.send(accounts.login_email(email, link, cfg))
    return LinkSent(minutes=cfg.login_link_minutes)


def _page(title: str, body: str, status: int = 200) -> HTMLResponse:
    """A small page for the links in emails, which open outside the web app."""
    return HTMLResponse(
        f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex"><title>{html.escape(title)} · Orbit Diff</title>
<style>
:root{{color-scheme:light dark;--bg:#f4f5f6;--card:#fff;--ink:#1c2126;--muted:#5c6670;
--line:#c3cad1}}
@media (prefers-color-scheme:dark){{:root{{--bg:#111417;--card:#1a1e22;--ink:#e8eaec;
--muted:#9aa4ad;--line:#3a4148}}}}
body{{margin:0;padding:48px 16px;background:var(--bg);color:var(--ink);
font:16px/1.5 -apple-system,'Segoe UI',Roboto,sans-serif}}
main{{max-width:480px;margin:0 auto;background:var(--card);border-radius:12px;padding:28px}}
h1{{font-size:20px;margin:0 0 12px}} p{{margin:0 0 16px}}
.muted{{color:var(--muted);font-size:14px}}
.pair{{display:flex;gap:8px;margin:0 0 16px}} .pair figure{{margin:0;flex:1}}
.pair img{{width:100%;border-radius:6px;display:block}}
figcaption{{font-size:12px;color:var(--muted);margin-top:4px}}
button,a.btn{{font:inherit;background:var(--ink);color:var(--card);border:0;border-radius:8px;
padding:10px 18px;cursor:pointer;text-decoration:none;display:inline-block}}
</style></head><body><main><h1>{html.escape(title)}</h1>{body}</main></body></html>""",
        status_code=status,
    )


def _home_link() -> str:
    return f'<a class="btn" href="{html.escape(settings().web_url)}">Open Orbit Diff</a>'


@app.get("/auth/callback")
def callback(token: str, s: SessionDep) -> Response:
    """Where a sign-in link lands: start a session and go to the web app."""
    cfg = pipeline_config().accounts
    used = accounts.use_login_link(s, token, cfg)
    if used is None:
        return _page(
            "This sign-in link has expired",
            f"<p>Links work for {cfg.login_link_minutes} minutes. Ask for a new one.</p>"
            + _home_link(),
            status=400,
        )
    _, session = used
    s.commit()
    response = RedirectResponse(settings().web_url, status_code=303)
    response.set_cookie(
        accounts.COOKIE,
        session,
        max_age=cfg.session_days * 86400,
        httponly=True,
        samesite="lax",
        secure=settings().web_url.startswith("https://"),
    )
    return response


@app.get("/auth/me")
def me(user: UserDep) -> Me:
    return Me(email=user.email, admin=_is_admin(user))


@app.get("/admin/summary")
def admin_summary(s: SessionDep, user: UserDep) -> monitoring.Summary:
    """Pipeline health and alert precision, in aggregate: no names, places or addresses."""
    if not _is_admin(user):
        raise HTTPException(403, "admins only")
    return monitoring.summary(s, pipeline_config(), datetime.now(UTC))


@app.post("/auth/logout", status_code=204)
def logout(s: SessionDep, response: Response, token: SessionCookie = None) -> None:
    if token:
        accounts.end_session(s, token)
        s.commit()
    response.delete_cookie(accounts.COOKIE)


def _feedback_target(s: Session, token: str) -> tuple[db.Event, db.Aoi, int] | None:
    payload = accounts.verify("feedback", token)
    if payload is None:
        return None
    event = s.get(db.Event, payload["e"])
    aoi = s.get(db.Aoi, event.aoi_id) if event else None
    if aoi is None or aoi.user_id != payload["u"]:  # the AOI is gone or changed hands
        return None
    return event, aoi, payload["u"]


def _data_uri(png: bytes) -> str:
    return f"data:image/png;base64,{base64.b64encode(png).decode()}"


@app.get("/feedback/{token}")
def feedback_page(token: str, s: SessionDep) -> HTMLResponse:
    """The "Not a real change" link in an alert email. It only asks: mail scanners open
    links before people do, so recording on GET would record what no one said."""
    target = _feedback_target(s, token)
    if target is None:
        return _page("This link no longer works", "<p>It has expired.</p>" + _home_link(), 400)
    event, aoi, _ = target
    what = notify.WHAT[event.category]
    images = {side: notify.event_image(aoi, event, side) for side in ("before", "after")}
    months = {"before": event.reference_month, "after": event.month}
    pair = "".join(
        f'<figure><img alt="{side}" src="{_data_uri(png)}">'
        f"<figcaption>{side.capitalize()} · {months[side]:%B %Y}</figcaption></figure>"
        for side, png in images.items()
        if png
    )
    return _page(
        "Not a real change?",
        f"<p>About {event.area_ha:.1f} ha of {html.escape(what)} in "
        f"{html.escape(aoi.name)}, {event.month:%B %Y}.</p>"
        f'<div class="pair">{pair}</div>'
        '<form method="post"><button type="submit">Yes, not a real change</button></form>'
        '<p class="muted" style="margin-top:16px">It tells us where the detector is wrong. '
        "It doesn't turn off alerts for this area.</p>",
    )


@app.post("/feedback/{token}")
def feedback_submit(token: str, s: SessionDep) -> HTMLResponse:
    target = _feedback_target(s, token)
    if target is None:
        return _page("This link no longer works", "<p>It has expired.</p>" + _home_link(), 400)
    event, _, user_id = target
    _record_not_real(s, event, user_id, via="email")
    return _page(
        "Thanks, noted",
        "<p>This change is recorded as not real, with the passes and settings that found it. "
        "That's what we tune the detector with.</p>" + _home_link(),
    )


@app.get("/health")
def health(s: SessionDep, redis: Annotated[Redis, Depends(get_redis)], response: Response) -> dict:
    checks = {}
    try:
        s.execute(text("SELECT 1"))
        checks["db"] = "ok"
    except Exception as exc:  # report, don't raise: this is the thing that says what's down
        checks["db"] = f"down: {type(exc).__name__}"
    try:
        redis.ping()
        checks["queue"] = "ok"
    except Exception as exc:
        checks["queue"] = f"down: {type(exc).__name__}"
    if any(v != "ok" for v in checks.values()):
        response.status_code = 503
    return checks
