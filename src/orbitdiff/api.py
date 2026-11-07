"""The REST API the web app talks to (Phase 4). Run: `uv run uvicorn orbitdiff.api:app`.

Accounts arrive in Phase 9. Until then every AOI is visible to every caller, so this
must not be exposed beyond a development machine.
"""

from collections.abc import Callable, Iterator
from datetime import UTC, date, datetime
from typing import Annotated, Literal

import numpy as np
from fastapi import Depends, FastAPI, HTTPException, Query, Response
from fastapi.middleware.cors import CORSMiddleware
from geoalchemy2.shape import from_shape, to_shape
from pydantic import BaseModel, Field
from redis import Redis
from shapely.geometry import mapping, shape
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from orbitdiff import cube, db, jobs, render, tiles
from orbitdiff.aoi import make_grid, reproject
from orbitdiff.config import pipeline_config, settings

app = FastAPI(title="Orbit Diff")
app.add_middleware(
    CORSMiddleware, allow_origins=settings().cors_origins, allow_methods=["*"], allow_headers=["*"]
)


def get_session() -> Iterator[Session]:
    with Session(db.engine(), expire_on_commit=False) as s:  # endpoints commit their writes
        yield s


def get_enqueue() -> Callable[[int], None]:
    return jobs.enqueue_backfill


def get_redis() -> Redis:
    return Redis.from_url(settings().redis_url, socket_timeout=2)


SessionDep = Annotated[Session, Depends(get_session)]


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


def _aoi(s: Session, aoi_id: int) -> db.Aoi:
    aoi = s.get(db.Aoi, aoi_id)
    if aoi is None:
        raise HTTPException(404, "no such AOI")
    return aoi


def _summary(aoi: db.Aoi) -> AoiSummary:
    return AoiSummary(id=aoi.id, name=aoi.name, area_ha=round(aoi.area_ha, 2), stage=aoi.stage)


@app.post("/aois", status_code=201)
def create_aoi(
    body: AoiCreate, s: SessionDep, enqueue: Annotated[Callable, Depends(get_enqueue)]
) -> AoiSummary:
    cfg = pipeline_config()
    try:
        grid = make_grid("new", shape(body.geometry.model_dump()), cfg.aoi.max_km2)
    except ValueError as exc:  # not a valid polygon, or over the size cap
        raise HTTPException(422, str(exc)) from exc
    aoi = db.Aoi(
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
    return _summary(aoi)


@app.get("/aois")
def list_aois(s: SessionDep) -> list[AoiSummary]:
    return [_summary(a) for a in s.scalars(select(db.Aoi).order_by(db.Aoi.id))]


@app.get("/aois/{aoi_id}")
def get_aoi(aoi_id: int, s: SessionDep) -> AoiDetail:
    aoi = _aoi(s, aoi_id)
    grid = jobs.grid_for(aoi)
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
        **_summary(aoi).model_dump(),
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
    aoi_id: int, s: SessionDep, enqueue: Annotated[Callable, Depends(get_enqueue)]
) -> Progress:
    """Retry a failed backfill, or pick up passes since the last one."""
    aoi = _aoi(s, aoi_id)
    if aoi.stage not in ("ready", "failed"):
        raise HTTPException(409, f"backfill already {aoi.stage}")
    aoi.stage, aoi.error = "queued", None
    s.commit()
    enqueue(aoi.id)
    return Progress(stage=aoi.stage, done=0, total=0, error=None)


@app.get("/aois/{aoi_id}/progress")
def progress(aoi_id: int, s: SessionDep) -> Progress:
    aoi = _aoi(s, aoi_id)
    return Progress(
        stage=aoi.stage, done=aoi.progress_done, total=aoi.progress_total, error=aoi.error
    )


def _months(s: Session, aoi_id: int) -> list[db.Month]:
    _aoi(s, aoi_id)
    return list(
        s.scalars(select(db.Month).where(db.Month.aoi_id == aoi_id).order_by(db.Month.month))
    )


@app.get("/aois/{aoi_id}/timeline")
def timeline(aoi_id: int, s: SessionDep) -> list[MonthOut]:
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
        for m in _months(s, aoi_id)
    ]


@app.get("/aois/{aoi_id}/ndvi")
def ndvi_series(aoi_id: int, s: SessionDep) -> list[NdviPoint]:
    return [
        NdviPoint(month=m.month, ndvi=m.ndvi_mean, observed_frac=m.observed_frac)
        for m in _months(s, aoi_id)
    ]


@app.get("/aois/{aoi_id}/scenes")
def scenes(
    aoi_id: int,
    s: SessionDep,
    month: Annotated[str, Query(pattern=r"^\d{4}-\d{2}$", description="YYYY-MM, UTC")],
) -> list[SceneOut]:
    _aoi(s, aoi_id)
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
    aoi_id: int, item_id: str, style: Literal["true_color", "ndvi"], s: SessionDep
) -> Response:
    """One pass over the AOI grid as the pipeline saw it, harmonized, from the cube.
    Place it on the map with the AOI's `corners`."""
    _aoi(s, aoi_id)
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
        headers={"Cache-Control": "public, max-age=31536000, immutable"},  # a pass never changes
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
