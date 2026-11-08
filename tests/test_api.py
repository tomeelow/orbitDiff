"""API and backfill job against a real PostGIS database.

Runs when ORBITDIFF_TEST_DATABASE_URL points at a database these tests may wipe (CI sets
it; see .env.example). Imagery comes from local files, as everywhere else.
"""

import json
import os
from collections import Counter
from datetime import UTC, datetime, timedelta
from urllib.error import URLError
from urllib.parse import parse_qs, urlsplit

import numpy as np
import pytest
import rasterio
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from fastapi.testclient import TestClient
from rasterio.io import MemoryFile
from redis.exceptions import ConnectionError as RedisConnectionError
from shapely.geometry import box, mapping
from sqlalchemy import create_engine, func, select, text

from orbitdiff import accounts, api, catalog, composites, db, detection, jobs, mail, tiles
from orbitdiff.aoi import make_grid
from orbitdiff.config import REPO_ROOT, pipeline_config, settings

TEST_DB = os.environ.get("ORBITDIFF_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not TEST_DB, reason="set ORBITDIFF_TEST_DATABASE_URL")


@pytest.fixture(scope="module")
def migrated():
    engine = create_engine(TEST_DB)
    with engine.begin() as c:
        c.execute(text("DROP SCHEMA public CASCADE"))
        c.execute(text("CREATE SCHEMA public"))
    alembic = Config(str(REPO_ROOT / "alembic.ini"))
    alembic.attributes["url"] = TEST_DB
    command.upgrade(alembic, "head")
    yield engine
    engine.dispose()


@pytest.fixture
def data_dir(migrated, tmp_path, monkeypatch):
    monkeypatch.setenv("ORBITDIFF_DATABASE_URL", TEST_DB)
    monkeypatch.setenv("ORBITDIFF_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("ORBITDIFF_SIGNING_KEY", "test-only-signing-key")
    settings.cache_clear()
    db.engine.cache_clear()
    with migrated.begin() as c:
        c.execute(text("TRUNCATE users, aois, login_links RESTART IDENTITY CASCADE"))
    yield tmp_path
    db.engine().dispose()
    db.engine.cache_clear()
    settings.cache_clear()


class FakeRedis:
    """As much of Redis as the API uses: counters, read in one transaction."""

    def __init__(self, up: bool = True) -> None:
        self.up = up
        self.counts: Counter[str] = Counter()
        self.expiring: dict[str, int] = {}
        self.queued: list[tuple] = []

    def ping(self) -> bool:
        if not self.up:
            raise RedisConnectionError("down")
        return True

    def pipeline(self) -> "FakeRedis":
        return self

    def incr(self, key: str) -> None:
        self.queued.append(("incr", key))

    def expire(self, key: str, seconds: int, nx: bool) -> None:
        self.queued.append(("expire", key, seconds, nx))

    def execute(self) -> list:
        self.ping()
        results: list = []
        for name, key, *rest in self.queued:
            if name == "incr":
                self.counts[key] += 1
                results.append(self.counts[key])
            else:
                self.expiring.setdefault(key, rest[0])
                results.append(True)
        self.queued.clear()
        return results


@pytest.fixture
def outbox(monkeypatch) -> list:
    """Every email sent, instead of sending it."""
    sent: list = []
    monkeypatch.setattr(mail, "send", sent.append)
    return sent


def sign_in(client, email: str) -> None:
    """The way a person signs in: the link from the email, opened."""
    with db.session() as s:
        token = accounts.new_login_link(s, email, pipeline_config().accounts)
    r = client.get("/auth/callback", params={"token": token}, follow_redirects=False)
    assert r.status_code == 303 and accounts.COOKIE in r.cookies


@pytest.fixture
def client(data_dir, outbox):
    queued: list[int] = []
    api.app.dependency_overrides[api.get_enqueue] = lambda: queued.append
    limiter = FakeRedis()
    api.app.dependency_overrides[api.get_redis] = lambda: limiter
    with TestClient(api.app) as c:
        c.queued = queued
        c.limiter = limiter
        c.outbox = outbox
        sign_in(c, "owner@example.com")
        yield c
    api.app.dependency_overrides.clear()


def runs(aoi_id: int) -> list[db.JobRun]:
    with db.session() as s:
        return list(
            s.scalars(select(db.JobRun).where(db.JobRun.aoi_id == aoi_id).order_by(db.JobRun.id))
        )


def create(client, polygon) -> dict:
    return client.post("/aois", json={"name": "Kyiv west", "geometry": mapping(polygon)})


def test_migrations_match_the_models(migrated):
    with migrated.connect() as c:
        context = MigrationContext.configure(
            c,
            opts={
                "include_object": lambda o, name, type_, r, ct: (
                    not (type_ == "table" and name in {"spatial_ref_sys", "topology", "layer"})
                )
            },
        )
        assert compare_metadata(context, db.Base.metadata) == []


def test_create_queues_a_backfill(client, aoi_polygon):
    r = create(client, aoi_polygon)
    assert r.status_code == 201
    aoi = r.json()
    assert aoi["stage"] == "queued" and 240 < aoi["area_ha"] < 250
    assert client.queued == [aoi["id"]]
    assert [a["id"] for a in client.get("/aois").json()] == [aoi["id"]]
    assert client.get(f"/aois/{aoi['id']}/progress").json() == {
        "stage": "queued",
        "done": 0,
        "total": 0,
        "error": None,
    }


def test_the_size_cap_is_enforced_at_the_api(client):
    r = create(client, box(30.0, 50.0, 31.0, 51.0))
    assert r.status_code == 422 and "cap" in r.json()["detail"]
    bowtie = {"type": "Polygon", "coordinates": [[[0, 0], [1, 1], [1, 0], [0, 1], [0, 0]]]}
    assert client.post("/aois", json={"name": "x", "geometry": bowtie}).status_code == 422
    point = {"type": "Point", "coordinates": [30.7, 50.39]}
    assert client.post("/aois", json={"name": "x", "geometry": point}).status_code == 422
    assert client.queued == []


def test_limits_report_the_configured_size_cap(client):
    assert client.get("/limits").json() == {"max_area_km2": pipeline_config().aoi.max_km2}


def test_unknown_aoi_is_404(client):
    for path in ("/aois/99", "/aois/99/progress", "/aois/99/timeline", "/aois/99/ndvi"):
        assert client.get(path).status_code == 404


@pytest.mark.filterwarnings("ignore::rasterio.errors.NotGeoreferencedWarning")  # PNGs
def test_backfill_end_to_end(client, aoi_polygon, make_scene, monkeypatch):
    found = [
        make_scene("aug", acquired=datetime(2021, 8, 15, 9, tzinfo=UTC)),
        make_scene("sep_clear", acquired=datetime(2021, 9, 3, 9, tzinfo=UTC)),
        make_scene("sep_cloudy", scl_class=9, acquired=datetime(2021, 9, 8, 9, tzinfo=UTC)),
        make_scene(  # no offset in the pixels, though the flag says there is (spike 5)
            "sep_bad_flag",
            band_value=150,
            baseline="05.12",
            offset_removed=False,
            acquired=datetime(2021, 9, 13, 9, tzinfo=UTC),
        ),
    ]
    monkeypatch.setattr(catalog, "search", lambda *a, **k: list(found))
    aoi_id = create(client, aoi_polygon).json()["id"]

    jobs.backfill(aoi_id, model=lambda red, green, nir: (red * 0).astype("uint8"))

    assert client.get(f"/aois/{aoi_id}/progress").json() == {
        "stage": "ready",
        "done": 4,
        "total": 4,
        "error": None,
    }
    detail = client.get(f"/aois/{aoi_id}").json()
    assert (detail["scenes"], detail["clear_scenes"]) == (4, 2)
    assert (detail["first_month"], detail["last_month"]) == ("2021-08-01", "2021-09-01")
    assert 0 < detail["bytes_read"] < detail["whole_files_bytes"]
    assert len(detail["corners"]) == 4

    timeline = client.get(f"/aois/{aoi_id}/timeline").json()
    assert [(m["month"], m["passes"], m["clear_scenes"], m["gap"]) for m in timeline] == [
        ("2021-08-01", 1, 1, False),
        ("2021-09-01", 3, 1, False),
    ]
    assert timeline[1]["tiles"]["true_color"].startswith(f"/aois/{aoi_id}/months/2021-09/tiles/")
    ndvi = client.get(f"/aois/{aoi_id}/ndvi").json()
    assert [p["ndvi"] for p in ndvi] == [0.0, 0.0]  # every test band reads 1500

    september = client.get(f"/aois/{aoi_id}/scenes", params={"month": "2021-09"}).json()
    assert [(s["item_id"], s["read_status"], s["offset_suspect"]) for s in september] == [
        ("sep_clear", "full", False),
        ("sep_cloudy", "scl", False),
        ("sep_bad_flag", "full", True),
    ]
    assert september[1]["images"] is None and september[1]["aoi_clear_frac"] == 0.0
    assert september[2]["aoi_clear_frac"] == 0.0  # never clear, so never in a composite
    image = client.get(september[0]["images"]["ndvi"])
    assert image.status_code == 200 and image.headers["content-type"] == "image/png"
    with MemoryFile(image.content) as mem, mem.open() as png:
        assert (png.count, png.height, png.width) == (4, 135, 197)
    assert client.get(f"/aois/{aoi_id}/scenes/sep_cloudy/true_color.png").status_code == 404

    # Rerunning finds nothing new and leaves the records as they were.
    client.post(f"/aois/{aoi_id}/backfill")
    jobs.backfill(aoi_id, model=lambda red, green, nir: (red * 0).astype("uint8"))
    assert client.get(f"/aois/{aoi_id}").json() == detail

    # Each run is recorded with what it read: everything, then nothing new.
    first, second = runs(aoi_id)
    assert (first.status, first.passes_read, first.passes_failed) == ("ok", 4, 0)
    assert first.bytes_read == detail["bytes_read"] and first.fallback_passes == 0
    assert (second.status, second.passes_read, second.bytes_read) == ("ok", 0, 0)
    assert first.finished_utc >= first.started_utc


def test_failed_backfill_is_recorded_and_can_be_retried(client, aoi_polygon, monkeypatch):
    def down(*args, **kwargs):
        raise RuntimeError("no catalog answered for 2017")

    monkeypatch.setattr(catalog, "search", down)
    aoi_id = create(client, aoi_polygon).json()["id"]
    with pytest.raises(RuntimeError):
        jobs.backfill(aoi_id, model=lambda *bands: None)
    progress = client.get(f"/aois/{aoi_id}/progress").json()
    assert progress["stage"] == "failed" and "no catalog answered" in progress["error"]
    (run,) = runs(aoi_id)
    assert run.status == "failed" and "no catalog answered" in run.error

    assert client.post(f"/aois/{aoi_id}/backfill").status_code == 202
    assert client.post(f"/aois/{aoi_id}/backfill").status_code == 409  # already queued
    assert client.queued == [aoi_id, aoi_id]


def test_health_reports_what_is_down(client):
    assert client.get("/health").json() == {"db": "ok", "queue": "ok"}
    api.app.dependency_overrides[api.get_redis] = lambda: FakeRedis(up=False)
    r = client.get("/health")
    assert r.status_code == 503 and r.json()["queue"].startswith("down")


def test_events_are_reconciled_never_duplicated_or_deleted(client, aoi_polygon, monkeypatch):
    aoi_id = create(client, aoi_polygon).json()["id"]
    grid = make_grid(str(aoi_id), aoi_polygon, max_km2=10)
    seen = np.datetime64("2021-05-05T09:00")

    def clearing(rows: slice, *statuses: tuple[str, str, str]) -> detection.Event:
        pixels = np.zeros((grid.height, grid.width), bool)
        pixels[rows, 10:30] = True
        e = detection.Event(
            "production",
            "2021-05",
            "2020-05",
            pixels,
            detection.footprint(pixels, grid),
            "vegetation_loss",
            0.82,
            0.18,
            seen,
        )
        for status, cause, at in statuses:
            e.set(status, cause, np.datetime64(at))
        return e

    found: list[detection.Event] = []
    monkeypatch.setattr(detection, "load_stack", lambda *a, **k: None)
    monkeypatch.setattr(detection, "Passes", lambda *a, **k: None)
    monkeypatch.setattr(detection, "detect", lambda *a, **k: list(found))

    def sync() -> list[dict]:
        jobs.sync_events(aoi_id, pipeline_config(), settings().data_dir)
        return client.get(f"/aois/{aoi_id}/events").json()

    found[:] = [clearing(slice(10, 20), ("pending", "S2A_a", "2021-05-05T09:00"))]
    (first,) = sync()
    assert (first["status"], first["category"], first["area_ha"]) == (
        "pending",
        "vegetation_loss",
        2.0,
    )
    assert first["month"] == "2021-05-01" and first["reference_month"] == "2020-05-01"
    assert first["history"][0]["cause"] == "S2A_a"
    assert first["geometry"]["type"] == "Polygon"
    assert first["method_version"].startswith("production-")
    assert (first["alerts"], first["alert_reason"]) == (True, "alert")  # 2 ha of forest gone

    # A new pass confirms it and its footprint grows a row: the same event, updated.
    found[:] = [
        clearing(
            slice(10, 21),
            ("pending", "S2A_a", "2021-05-05T09:00"),
            ("confirmed", "S2B_b", "2021-05-08T09:00"),
        )
    ]
    (second,) = sync()
    assert second["id"] == first["id"]
    assert (second["status"], second["area_ha"]) == ("confirmed", 2.2)
    assert second["status_utc"].startswith("2021-05-08")

    # Settings change and it is no longer found: dismissed, and its record kept.
    found[:] = []
    (third,) = sync()
    assert (third["id"], third["status"]) == (first["id"], "dismissed")
    assert third["history"][-1]["cause"] == "no longer detected"
    # It alerted while pending, before anything dismissed it: that false alarm still counts.
    assert (third["alerts"], third["alert_reason"]) == (True, "alert")


def test_a_recomputed_event_keeps_its_own_row_not_a_stale_neighbour(
    client, aoi_polygon, monkeypatch
):
    aoi_id = create(client, aoi_polygon).json()["id"]
    grid = make_grid(str(aoi_id), aoi_polygon, max_km2=10)

    def event(rows: slice) -> detection.Event:
        pixels = np.zeros((grid.height, grid.width), bool)
        pixels[rows, 10:30] = True
        e = detection.Event(
            "production",
            "2021-05",
            "2020-05",
            pixels,
            detection.footprint(pixels, grid),
            "vegetation_loss",
            0.8,
            0.2,
            np.datetime64("2021-05-05T09:00"),
        )
        e.set("pending", "S2A_a", np.datetime64("2021-05-05T09:00"))
        return e

    found: list[detection.Event] = []
    monkeypatch.setattr(detection, "load_stack", lambda *a, **k: None)
    monkeypatch.setattr(detection, "Passes", lambda *a, **k: None)
    monkeypatch.setattr(detection, "detect", lambda *a, **k: list(found))

    def sync() -> dict[int, dict]:
        jobs.sync_events(aoi_id, pipeline_config(), settings().data_dir)
        return {e["id"]: e for e in client.get(f"/aois/{aoi_id}/events").json()}

    found[:] = [event(slice(21, 31))]
    (stale,) = sync()  # stored first, so it comes first when rows are scanned
    found[:] = [event(slice(10, 20))]
    events = sync()
    (own,) = set(events) - {stale}
    assert events[stale]["history"][-1]["cause"] == "no longer detected"

    # It grows a row and now touches the stale footprint: it still keeps its own row.
    found[:] = [event(slice(10, 21))]
    events = sync()
    assert set(events) == {stale, own}
    assert (events[own]["status"], events[own]["area_ha"]) == ("pending", 2.2)
    assert events[stale]["status"] == "dismissed"


def test_sign_in_by_emailed_link(data_dir, outbox):
    api.app.dependency_overrides[api.get_redis] = FakeRedis
    with TestClient(api.app) as c:
        assert c.get("/auth/me").status_code == 401
        assert c.post("/auth/login", json={"email": "not an address"}).status_code == 422
        r = c.post("/auth/login", json={"email": " Me@Example.com "})
        assert r.status_code == 202
        assert r.json() == {"minutes": pipeline_config().accounts.login_link_minutes}
        (message,) = outbox
        assert message["To"] == "me@example.com"
        link = next(w for w in message.get_content().split() if "/auth/callback?token=" in w)
        r = c.get(urlsplit(link)._replace(scheme="", netloc="").geturl(), follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == settings().web_url
        cookie = r.headers["set-cookie"].lower()
        assert "httponly" in cookie and "samesite=lax" in cookie
        assert c.get("/auth/me").json() == {"email": "me@example.com", "admin": False}

        assert c.post("/auth/logout").status_code == 204
        assert c.get("/auth/me").status_code == 401
        assert c.get("/auth/callback", params={"token": "made-up"}).status_code == 400
    api.app.dependency_overrides.clear()


def test_an_aoi_is_only_ever_seen_by_its_owner(client, aoi_polygon):
    aoi_id = create(client, aoi_polygon).json()["id"]
    paths = [
        f"/aois/{aoi_id}",
        f"/aois/{aoi_id}/progress",
        f"/aois/{aoi_id}/timeline",
        f"/aois/{aoi_id}/ndvi",
        f"/aois/{aoi_id}/scenes?month=2021-09",
        f"/aois/{aoi_id}/events",
        f"/aois/{aoi_id}/alert-rule",
    ]
    assert all(client.get(p).status_code == 200 for p in paths)

    client.cookies.clear()
    assert all(client.get(p).status_code == 401 for p in paths)
    assert client.get("/aois").status_code == 401

    sign_in(client, "someone-else@example.com")
    assert client.get("/aois").json() == []
    assert all(client.get(p).status_code == 404 for p in paths)  # not 403: ids reveal nothing
    assert client.post(f"/aois/{aoi_id}/backfill").status_code == 404


def test_sign_in_links_are_limited_per_address(client):
    limit = pipeline_config().accounts.login_per_email
    for _ in range(limit):
        assert client.post("/auth/login", json={"email": "a@example.com"}).status_code == 202
    assert len(client.outbox) == limit

    refused = client.post("/auth/login", json={"email": "A@example.com "})  # the same address
    assert refused.status_code == 429 and refused.headers["retry-after"] == "3600"
    assert len(client.outbox) == limit  # nothing more was sent
    assert not any("a@example.com" in key for key in client.limiter.counts)  # hashed
    assert set(client.limiter.expiring.values()) == {3600}

    assert client.post("/auth/login", json={"email": "b@example.com"}).status_code == 202


def test_sign_in_links_are_limited_per_client(client):
    limit = pipeline_config().accounts.login_per_ip
    for i in range(limit):
        assert client.post("/auth/login", json={"email": f"{i}@example.com"}).status_code == 202
    assert client.post("/auth/login", json={"email": "next@example.com"}).status_code == 429


def test_sign_in_stops_rather_than_sending_unlimited_when_redis_is_down(client):
    client.limiter.up = False
    assert client.post("/auth/login", json={"email": "a@example.com"}).status_code == 503
    assert client.outbox == []


def test_map_tiles_come_through_the_api_for_the_owner_only(client, aoi_polygon, monkeypatch):
    aoi_id = create(client, aoi_polygon).json()["id"]
    asked: list[str] = []

    def titiler(url):
        asked.append(url)
        return 200, b"tile", "image/png"

    monkeypatch.setattr(tiles, "fetch", titiler)

    # A caller can't choose the file: `url` is built on the server.
    r = client.get(
        f"/aois/{aoi_id}/months/2021-09/tiles/ndvi/14/3/5.png",
        params={"url": "/data/composites/999/2021-09_display.tif", "v": "abc"},
    )
    assert (r.status_code, r.content, r.headers["content-type"]) == (200, b"tile", "image/png")
    assert r.headers["cache-control"].startswith("private")
    assert parse_qs(urlsplit(asked[0]).query)["url"] == [
        f"/data/composites/{aoi_id}/2021-09_display.tif"
    ]
    point = client.get(f"/aois/{aoi_id}/months/2021-09/point/30.5,50.25")
    assert point.status_code == 200 and urlsplit(asked[1]).path == "/cog/point/30.5,50.25"

    assert client.get(f"/aois/{aoi_id}/months/2021-13/point/30.5,50.25").status_code == 422
    assert client.get(f"/aois/{aoi_id}/months/2021-09/tiles/rgb/1/1/1.png").status_code == 422

    asked.clear()
    path = f"/aois/{aoi_id}/months/2021-09/tiles/ndvi/14/3/5.png"
    sign_in(client, "someone.else@example.com")
    assert client.get(path).status_code == 404
    client.cookies.clear()
    assert client.get(path).status_code == 401
    assert asked == []  # neither got as far as TiTiler


def test_a_tile_server_problem_is_a_clean_error(client, aoi_polygon, monkeypatch):
    aoi_id = create(client, aoi_polygon).json()["id"]
    path = f"/aois/{aoi_id}/months/2021-09/tiles/ndvi/14/3/5.png"

    monkeypatch.setattr(tiles, "fetch", lambda url: (404, b"", ""))
    assert client.get(path).status_code == 404
    monkeypatch.setattr(tiles, "fetch", lambda url: (500, b"", ""))
    assert client.get(path).status_code == 502

    def unreachable(url):
        raise URLError("connection refused")

    monkeypatch.setattr(tiles, "fetch", unreachable)
    assert client.get(path).status_code == 502


def test_assign_aois_gives_ownerless_aois_an_owner(client, aoi_polygon, capsys):
    from orbitdiff import cli

    aoi_id = create(client, aoi_polygon).json()["id"]
    with db.session() as s:
        s.get(db.Aoi, aoi_id).user_id = None  # drawn before accounts existed
    assert client.get("/aois").json() == []
    cli.main(["assign-aois", "--email", "Owner@Example.com"])
    assert [a["id"] for a in client.get("/aois").json()] == [aoi_id]


def make_event(grid, rows: slice, month: str, *statuses: tuple[str, str, str]) -> detection.Event:
    """A clearing over `rows`, first seen in `month`, then through `statuses`."""
    pixels = np.zeros((grid.height, grid.width), bool)
    pixels[rows, 10:30] = True
    year, m = month.split("-")
    first = np.datetime64(statuses[0][2])
    e = detection.Event(
        "production",
        month,
        f"{int(year) - 1}-{m}",
        pixels,
        detection.footprint(pixels, grid),
        "vegetation_loss",
        0.82,
        0.18,
        first,
    )
    for status, cause, at in statuses:
        e.set(status, cause, np.datetime64(at))
    return e


@pytest.fixture
def detected(client, aoi_polygon, monkeypatch):
    """An AOI whose detector finds whatever the test puts in `found`."""
    aoi_id = create(client, aoi_polygon).json()["id"]
    grid = make_grid(str(aoi_id), aoi_polygon, max_km2=10)
    found: list[detection.Event] = []
    monkeypatch.setattr(detection, "load_stack", lambda *a, **k: None)
    monkeypatch.setattr(detection, "Passes", lambda *a, **k: None)
    monkeypatch.setattr(detection, "detect", lambda *a, **k: list(found))

    def sync(*events: detection.Event) -> None:
        found[:] = events
        jobs.sync_events(aoi_id, pipeline_config(), settings().data_dir)

    return aoi_id, grid, sync


RULE = {
    "enabled": False,
    "categories": ["buildings_roads", "vegetation_loss"],
    "skip_seasonal": True,
    "min_ha": 0.5,
}


def test_alert_rule_is_checked_saved_and_previewed(client, detected):
    aoi_id, grid, sync = detected
    rule = client.get(f"/aois/{aoi_id}/alert-rule").json()
    assert rule["enabled"] is False and rule["watching_since"] is None
    assert (rule["min_ha"], rule["min_ha_range"]) == (0.5, [0.1, 5.0])
    assert rule["categories"] == ["buildings_roads", "vegetation_loss", "solar_industry"]

    for bad in ({"min_ha": 0.05}, {"min_ha": 6}, {"categories": ["clouds"]}):
        assert client.put(f"/aois/{aoi_id}/alert-rule", json=RULE | bad).status_code == 422

    sync(
        # 2 ha, confirmed: alerts under the rule.
        make_event(
            grid,
            slice(10, 20),
            "2021-05",
            ("pending", "A", "2021-05-05"),
            ("confirmed", "B", "2021-05-08"),
        ),
        # Alerted while pending, then a clear pass didn't see it: a false alarm, counted.
        make_event(
            grid,
            slice(40, 50),
            "2021-06",
            ("pending", "C", "2021-06-05"),
            ("dismissed", "D", "2021-06-09"),
        ),
        # 0.4 ha: under the smallest change.
        make_event(grid, slice(60, 62), "2021-07", ("pending", "E", "2021-07-05")),
    )
    preview = client.post(f"/aois/{aoi_id}/alert-rule/preview", json=RULE).json()
    assert preview == {"changes": 3, "would_alert": 2, "retracted": 1}
    smaller = RULE | {"min_ha": 0.1}
    assert (
        client.post(f"/aois/{aoi_id}/alert-rule/preview", json=smaller).json()["would_alert"] == 3
    )

    saved = client.put(f"/aois/{aoi_id}/alert-rule", json=RULE | {"enabled": True}).json()
    assert saved["enabled"] and saved["watching_since"] is not None
    assert client.get(f"/aois/{aoi_id}").json()["alerts_on"] is True
    events = client.get(f"/aois/{aoi_id}/events").json()
    assert [(e["alerts"], e["alert_reason"]) for e in events] == [
        (False, "under 0.5 ha"),
        (True, "alert"),
        (True, "alert"),
    ]
    off = client.put(f"/aois/{aoi_id}/alert-rule", json=RULE).json()
    assert off["enabled"] is False and off["watching_since"] is None


def watch_from(aoi_id: int, since: str) -> None:
    with db.session() as s:
        s.get(db.AlertRule, aoi_id).watching_since = datetime.fromisoformat(since).replace(
            tzinfo=UTC
        )


def notifications(aoi_id: int) -> list[tuple[str, str, str, int]]:
    with db.session() as s:
        rows = s.scalars(select(db.Notification).order_by(db.Notification.id))
        return [(n.kind, n.event_status, n.state, n.attempts) for n in rows]


def test_an_event_alerts_once_and_follows_up_once(client, detected):
    from orbitdiff import notify

    aoi_id, grid, sync = detected
    client.put(f"/aois/{aoi_id}/alert-rule", json=RULE | {"enabled": True})
    watch_from(aoi_id, "2021-05-01")
    old = make_event(grid, slice(40, 50), "2021-04", ("pending", "A", "2021-04-10"))
    seen = ("pending", "S2A_new", "2021-05-05T09:00")

    sync(old, make_event(grid, slice(10, 20), "2021-05", seen))
    assert notify.notify(aoi_id) == 1  # the April change was in the archive already
    (alert,) = client.outbox
    assert alert["Subject"] == "Possible vegetation loss in Kyiv west (2.0 ha)"
    text_part = alert.get_body(("plain",)).get_content()
    assert "5 May 2021" in text_part
    assert "next clear pass confirms or dismisses it" in text_part
    assert f"/#/areas/{aoi_id}/changes/" in text_part and "/feedback/" in text_part
    assert notify.notify(aoi_id) == 0  # a retried job sends nothing twice

    confirmed = ("confirmed", "S2B_next", "2021-05-08T09:00")
    sync(old, make_event(grid, slice(10, 20), "2021-05", seen, confirmed))
    assert notify.notify(aoi_id) == 1 and notify.notify(aoi_id) == 0
    assert client.outbox[1]["Subject"] == "Confirmed: vegetation loss in Kyiv west (2.0 ha)"

    sync(old)  # no longer found: dismissed, but the one follow-up has gone out
    assert notify.notify(aoi_id) == 0
    assert notifications(aoi_id) == [
        ("alert", "pending", "sent", 1),
        ("update", "confirmed", "sent", 1),
    ]


def clearing_at(grid, top: int, month: str = "2021-05") -> detection.Event:
    return make_event(grid, slice(top, top + 5), month, ("pending", "S2A_new", f"{month}-05"))


def test_many_changes_at_once_arrive_as_a_digest_and_still_only_once(client, detected):
    from orbitdiff import notify

    aoi_id, grid, sync = detected
    client.put(f"/aois/{aoi_id}/alert-rule", json=RULE | {"enabled": True})
    watch_from(aoi_id, "2021-05-01")
    alerts = pipeline_config().alerts
    changes = alerts.digest_size + 2
    sync(*(clearing_at(grid, 10 * i) for i in range(changes)))

    assert notify.notify(aoi_id) == changes
    first, second = client.outbox
    assert first["Subject"] == f"{alerts.digest_size} changes in Kyiv west"
    assert second["Subject"] == "2 changes in Kyiv west"
    text = first.get_body(("plain",)).get_content()
    assert text.count("/feedback/") == alerts.digest_size  # each change can be disputed
    assert text.count("Possible vegetation loss") >= alerts.digest_size
    assert notifications(aoi_id) == [("alert", "pending", "sent", 1)] * changes
    assert notify.notify(aoi_id) == 0 and len(client.outbox) == 2  # a retry sends nothing


def test_a_few_changes_at_once_stay_separate_emails(client, detected):
    from orbitdiff import notify

    aoi_id, grid, sync = detected
    client.put(f"/aois/{aoi_id}/alert-rule", json=RULE | {"enabled": True})
    watch_from(aoi_id, "2021-05-01")
    sync(*(clearing_at(grid, 10 * i) for i in range(pipeline_config().alerts.digest_over)))
    assert notify.notify(aoi_id) == len(client.outbox) == pipeline_config().alerts.digest_over


def test_a_digest_that_fails_to_send_is_retried_for_every_change_in_it(
    client, detected, monkeypatch
):
    from orbitdiff import notify

    aoi_id, grid, sync = detected
    client.put(f"/aois/{aoi_id}/alert-rule", json=RULE | {"enabled": True})
    watch_from(aoi_id, "2021-05-01")
    changes = pipeline_config().alerts.digest_over + 1
    sync(*(clearing_at(grid, 10 * i) for i in range(changes)))
    sent = client.outbox

    def down(message):
        raise ConnectionRefusedError("smtp down")

    monkeypatch.setattr(mail, "send", down)
    assert notify.notify(aoi_id) == 0
    assert notifications(aoi_id) == [("alert", "pending", "failed", 1)] * changes

    monkeypatch.setattr(mail, "send", sent.append)
    assert notify.notify(aoi_id) == changes and len(sent) == 1
    assert notifications(aoi_id) == [("alert", "pending", "sent", 2)] * changes


def test_a_seasonal_change_someone_asked_to_hear_about_is_not_called_confirmed(client, detected):
    from orbitdiff import notify

    aoi_id, grid, sync = detected
    rule = RULE | {"enabled": True, "skip_seasonal": False}
    client.put(f"/aois/{aoi_id}/alert-rule", json=rule)
    watch_from(aoi_id, "2021-05-01")
    seen = ("seasonal", "looked like this in 2020-05", "2021-05-05")
    sync(make_event(grid, slice(10, 20), "2021-05", seen))
    notify.notify(aoi_id)
    (email,) = client.outbox
    assert email["Subject"] == "Seasonal: vegetation loss in Kyiv west (2.0 ha)"
    text = email.get_body(("plain",)).get_content()
    assert "looks seasonal (looked like this in May 2020)" in text
    assert "seen on 1 clear pass" in text and "confirmed" not in text


def test_every_alert_says_how_sure_it_is(client, detected):
    from orbitdiff import notify

    aoi_id, grid, sync = detected
    client.put(f"/aois/{aoi_id}/alert-rule", json=RULE | {"enabled": True})
    watch_from(aoi_id, "2021-05-01")
    event = clearing_at(grid, 10)
    event.shares = [0.9, 0.7]
    sync(event)
    assert client.get(f"/aois/{aoi_id}/events").json()[0]["confidence"] == 0.8
    notify.notify(aoi_id)
    text = client.outbox[0].get_body(("plain",)).get_content()
    assert "Confidence: 80% of it changed on the clear passes that decided it" in text


def test_a_failed_send_is_retried_then_given_up(client, detected, monkeypatch):
    from orbitdiff import notify

    aoi_id, grid, sync = detected
    client.put(f"/aois/{aoi_id}/alert-rule", json=RULE | {"enabled": True})
    watch_from(aoi_id, "2021-05-01")
    sync(make_event(grid, slice(10, 20), "2021-05", ("pending", "S2A_new", "2021-05-05")))

    def down(message):
        raise ConnectionRefusedError("smtp down")

    monkeypatch.setattr(mail, "send", down)
    attempts = pipeline_config().alerts.send_attempts
    for _ in range(attempts + 1):
        assert notify.notify(aoi_id) == 0
    assert notifications(aoi_id) == [("alert", "pending", "failed", attempts)]


def test_not_a_real_change_from_the_email_and_from_the_app(client, detected):
    from orbitdiff import notify

    aoi_id, grid, sync = detected
    client.put(f"/aois/{aoi_id}/alert-rule", json=RULE | {"enabled": True})
    watch_from(aoi_id, "2021-05-01")
    sync(make_event(grid, slice(10, 20), "2021-05", ("pending", "S2A_new", "2021-05-05")))
    notify.notify(aoi_id)
    text_part = client.outbox[0].get_body(("plain",)).get_content()
    link = next(w for w in text_part.split() if "/feedback/" in w)
    path = urlsplit(link).path

    client.cookies.clear()  # the link works without signing in
    page = client.get(path)
    assert page.status_code == 200 and 'method="post"' in page.text
    assert notifications(aoi_id)  # opening the page records nothing
    with db.session() as s:
        assert s.scalar(select(func.count()).select_from(db.Feedback)) == 0
    assert client.post(path).status_code == 200
    assert client.post(path).status_code == 200  # twice is still once
    body, _, mac = path.removeprefix("/feedback/").partition(".")
    assert client.get(f"/feedback/{body}.{mac[::-1]}").status_code == 400  # tampered

    with db.session() as s:
        (row,) = s.scalars(select(db.Feedback)).all()
        assert (row.verdict, row.via) == ("not_real", "email")
        assert row.event["history"][0]["cause"] == "S2A_new" and row.event["method_version"]

    sign_in(client, "owner@example.com")
    (event,) = client.get(f"/aois/{aoi_id}/events").json()
    assert event["not_real"] is True
    r = client.post(f"/aois/{aoi_id}/events/{event['id']}/feedback")
    assert r.status_code == 204  # already recorded from the email: still one row


def test_polling_queues_each_watched_idle_aoi_once(client, aoi_polygon):
    watched = create(client, aoi_polygon).json()["id"]
    unwatched = create(client, aoi_polygon).json()["id"]
    client.put(f"/aois/{watched}/alert-rule", json=RULE | {"enabled": True})
    queued: list[int] = []

    assert jobs.poll_watched(queued.append) == []  # still on its first backfill
    with db.session() as s:
        s.get(db.Aoi, watched).stage = "ready"
        s.get(db.Aoi, unwatched).stage = "ready"
    assert jobs.poll_watched(queued.append) == [watched]
    assert jobs.poll_watched(queued.append) == []  # queued already: not twice
    assert queued == [watched]


def test_the_admin_view_measures_precision_and_health_for_admins_only(
    client, detected, monkeypatch
):
    from orbitdiff import monitoring, notify

    aoi_id, grid, sync = detected
    assert client.get("/auth/me").json()["admin"] is False
    assert client.get("/admin/summary").status_code == 403
    monkeypatch.setattr(settings(), "admin_emails", ["Owner@Example.com"])
    assert client.get("/auth/me").json()["admin"] is True

    client.put(f"/aois/{aoi_id}/alert-rule", json=RULE | {"enabled": True})
    watch_from(aoi_id, "2021-05-01")
    sync(
        make_event(grid, slice(10, 20), "2021-05", ("pending", "A", "2021-05-05T09:00")),
        make_event(grid, slice(40, 50), "2021-05", ("pending", "B", "2021-05-06T09:00")),
    )
    assert notify.notify(aoi_id) == 2
    first, _ = client.get(f"/aois/{aoi_id}/events").json()
    client.post(f"/aois/{aoi_id}/events/{first['id']}/feedback")
    with db.session() as s:
        s.add_all(
            [
                db.JobRun(
                    aoi_id=aoi_id,
                    started_utc=datetime.now(UTC) - timedelta(minutes=10),
                    finished_utc=datetime.now(UTC),
                    status="failed",
                    error="RuntimeError: no catalog answered",
                    passes_read=3,
                    passes_failed=1,
                    fallback_passes=2,
                    read_retries=4,
                    search_errors=1,
                    search_fallbacks=1,
                    bytes_read=1000,
                    emails_sent=0,
                ),
            ]
        )

    summary = client.get("/admin/summary").json()
    (row,) = summary["precision"]
    assert (row["category"], row["alerts"], row["not_real"], row["precision"]) == (
        "vegetation_loss",
        2,
        1,
        0.5,
    )
    assert row["flagged"] is False  # under min_alerts: too few to say
    health = summary["health"]
    assert (health["runs"], health["failed_runs"], health["fallback_passes"]) == (1, 1, 2)
    assert (health["read_retries"], health["search_errors"], health["search_fallbacks"]) == (
        4,
        1,
        1,
    )
    assert health["median_run_minutes"] == pytest.approx(10, abs=0.1)
    assert (health["emails_sent"], health["emails_stuck"], health["feedback"]) == (2, 0, 1)
    assert health["median_alert_hours"] > 24 * 365  # passes from 2021, sent today
    assert summary["failures"][0]["aoi_id"] == aoi_id
    assert summary["production_version"].startswith("production-")
    assert "owner@example.com" not in str(summary)  # aggregate only

    # Enough alerts below the bar flag the category for review.
    cfg = pipeline_config()
    strict = cfg.model_copy(
        update={"monitoring": cfg.monitoring.model_copy(update={"min_alerts": 2})}
    )
    with db.session() as s:
        (flagged,) = monitoring.summary(s, strict, datetime.now(UTC)).precision
    assert flagged.flagged


def test_the_admin_view_shows_the_latest_evaluation_and_whether_to_trust_it(
    client, monkeypatch, tmp_path
):
    from orbitdiff import evaluation

    monkeypatch.setattr(settings(), "admin_emails", ["owner@example.com"])
    monkeypatch.setattr(settings(), "data_dir", tmp_path)
    assert client.get("/admin/summary").json()["evaluation"] is None

    scores = evaluation.Scores(alerts=4, true_alerts=3, labels=4, found=3)
    report = evaluation.Report(scores, {}, {})
    evaluation.save_scored({"production": report}, "abc123", False, tmp_path)
    scored = client.get("/admin/summary").json()["evaluation"]
    assert (scored["eval_set"], scored["verified"]) == ("abc123", False)
    (row,) = scored["rows"]
    assert (row["method"], row["precision"], row["recall"]) == ("production", 0.75, 0.75)


def write_composite(data_dir, aoi_id: int, month: str, grid) -> None:
    folder = composites.composite_dir(data_dir, str(aoi_id))
    folder.mkdir(parents=True, exist_ok=True)
    data = np.random.default_rng(int(month[:4])).integers(500, 3000, (6, grid.height, grid.width))
    profile = {"driver": "GTiff", "width": grid.width, "height": grid.height, "count": 6}
    with rasterio.open(folder / f"{month}.tif", "w", dtype="uint16", **profile) as dst:
        dst.write(data.astype("uint16"))


@pytest.fixture
def an_empty_eval_set(monkeypatch, tmp_path):
    from orbitdiff import evaluation

    folder = tmp_path / "eval"
    folder.mkdir()
    monkeypatch.setattr(evaluation, "EVAL_DIR", folder)
    return folder


def test_not_a_real_change_trains_on_its_footprint_only(client, detected, an_empty_eval_set):
    from orbitdiff import retraining

    aoi_id, grid, sync = detected
    sync(make_event(grid, slice(10, 20), "2021-05", ("pending", "S2A_new", "2021-05-05")))
    (event,) = client.get(f"/aois/{aoi_id}/events").json()
    client.post(f"/aois/{aoi_id}/events/{event['id']}/feedback")
    data_dir = settings().data_dir
    for month in ("2020-05", "2021-05"):
        write_composite(data_dir, aoi_id, month, grid)

    with db.session() as s:
        (item,) = retraining.load_not_real(s, data_dir)

    assert (item.event_id, item.aoi_id) == (event["id"], aoi_id)
    assert item.before.shape == (4, grid.height, grid.width)
    assert not np.array_equal(item.before, item.after)  # the two months, not one twice
    expected = np.zeros((grid.height, grid.width), bool)
    expected[10:20, 10:30] = True
    np.testing.assert_array_equal(item.footprint, expected)


def test_feedback_without_composites_is_skipped(client, detected, an_empty_eval_set):
    from orbitdiff import retraining

    aoi_id, grid, sync = detected
    sync(make_event(grid, slice(10, 20), "2021-05", ("pending", "S2A_new", "2021-05-05")))
    (event,) = client.get(f"/aois/{aoi_id}/events").json()
    client.post(f"/aois/{aoi_id}/events/{event['id']}/feedback")
    with db.session() as s:
        assert retraining.load_not_real(s, settings().data_dir) == []


def test_an_area_in_the_evaluation_set_is_never_trained_on(
    client, detected, aoi_polygon, an_empty_eval_set
):
    from orbitdiff import retraining

    aoi_id, grid, sync = detected
    sync(make_event(grid, slice(10, 20), "2021-05", ("pending", "S2A_new", "2021-05-05")))
    (event,) = client.get(f"/aois/{aoi_id}/events").json()
    client.post(f"/aois/{aoi_id}/events/{event['id']}/feedback")
    data_dir = settings().data_dir
    for month in ("2020-05", "2021-05"):
        write_composite(data_dir, aoi_id, month, grid)
    area = {
        "type": "Feature",
        "properties": {
            "role": "aoi",
            "first_month": "2019-01",
            "last_month": "2023-12",
        },
        "geometry": mapping(aoi_polygon.buffer(-0.001)),
    }
    (an_empty_eval_set / "overlapping.geojson").write_text(
        json.dumps({"type": "FeatureCollection", "features": [area]})
    )
    with db.session() as s:
        assert retraining.load_not_real(s, data_dir) == []


def test_training_refuses_to_run_blind_to_the_evaluation_set(data_dir, monkeypatch):
    from orbitdiff import evaluation, retraining

    monkeypatch.setattr(evaluation, "EVAL_DIR", data_dir / "not-here")
    with db.session() as s, pytest.raises(RuntimeError, match="evaluation set"):
        retraining.load_not_real(s, data_dir)


def buildings_flagged(client, detected, monkeypatch, labels: int, version: str):
    """Sent building alerts under `version`, `labels` of them marked not real."""
    from orbitdiff import notify

    aoi_id, grid, sync = detected
    client.put(f"/aois/{aoi_id}/alert-rule", json=RULE | {"enabled": True})
    watch_from(aoi_id, "2021-05-01")
    events = [
        make_event(grid, slice(i, i + 10), "2021-05", ("pending", f"S2A_{i}", "2021-05-05"))
        for i in range(0, 12 * labels, 12)
    ]
    for event in events:
        event.category = "buildings_roads"
    sync(*events)
    with db.session() as s:
        for event in s.scalars(select(db.Event)):
            event.method_version = version
    assert notify.notify(aoi_id) == labels
    for event in client.get(f"/aois/{aoi_id}/events").json():
        client.post(f"/aois/{aoi_id}/events/{event['id']}/feedback")
    cfg = pipeline_config()
    strict = cfg.model_copy(
        update={
            "monitoring": cfg.monitoring.model_copy(
                update={"min_alerts": 2, "retrain_min_new_labels": 2}
            )
        }
    )
    monkeypatch.setattr(jobs, "pipeline_config", lambda: strict)


def test_a_flagged_building_model_queues_one_candidate_until_there_is_more_to_learn(
    client, detected, monkeypatch
):
    from orbitdiff import buildings

    buildings_flagged(client, detected, monkeypatch, 3, "production-1a2b+orbitdiff_buildings/1")
    data_dir = settings().data_dir
    queued: list[int] = []

    assert jobs.poll_retraining(lambda: queued.append(1)) is False  # no champion: nothing runs
    buildings.write_champion(data_dir, "orbitdiff_buildings", 1, "run")
    assert jobs.poll_retraining(lambda: queued.append(1)) is True
    assert jobs.poll_retraining(lambda: queued.append(1)) is False  # no new labels since
    assert len(queued) == 1
    assert buildings.candidate_info(data_dir)["labels"] == 3
    assert buildings.champion_info(data_dir)["version"] == 1  # the champion is untouched
    monkeypatch.setattr(settings(), "admin_emails", ["owner@example.com"])
    assert client.get("/admin/summary").json()["retraining"]["labels"] == 3


def test_rules_going_wrong_is_not_a_reason_to_retrain_the_model(client, detected, monkeypatch):
    from orbitdiff import buildings

    buildings_flagged(client, detected, monkeypatch, 3, "production-1a2b3c4d")  # no model in it
    buildings.write_champion(settings().data_dir, "orbitdiff_buildings", 1, "run")
    assert jobs.poll_retraining(lambda: pytest.fail("queued")) is False


def test_labels_are_saved_to_eval_for_admins_only(client, aoi_polygon, monkeypatch, tmp_path):
    from orbitdiff import evaluation

    folder = tmp_path / "eval"
    folder.mkdir()
    monkeypatch.setattr(evaluation, "EVAL_DIR", folder)
    aoi_id = create(client, aoi_polygon).json()["id"]
    assert client.get(f"/aois/{aoi_id}/labels").status_code == 403
    monkeypatch.setattr(settings(), "admin_emails", ["owner@example.com"])

    fresh = client.get(f"/aois/{aoi_id}/labels").json()
    assert fresh["name"] == "kyiv-west" and fresh["saved"] is False and fresh["changes"] == []
    minx, miny, maxx, maxy = aoi_polygon.bounds
    half_out = box(maxx - 0.005, miny, maxx + 0.005, maxy)  # the change is clipped to the area
    body = {
        "first_month": "2017-01",
        "last_month": "2024-12",
        "hard_cases": ["cropland"],
        "changes": [
            {"category": "buildings_roads", "month": "2019-06", "geometry": mapping(half_out)}
        ],
    }
    saved = client.put(f"/aois/{aoi_id}/labels", json=body).json()
    assert saved["saved"] is True and saved["changes"][0]["month"] == "2019-06"
    (site,) = evaluation.load_eval_set(folder)[0]
    assert site.name == "kyiv-west" and site.hard_cases == ("cropland",) and not site.verified
    assert site.labels[0].footprint.within(aoi_polygon.buffer(1e-6))
    assert client.get(f"/aois/{aoi_id}/labels").json() == saved

    def refused(change: dict | None = None, **fields) -> str:
        changes = [body["changes"][0] | (change or {})]
        r = client.put(f"/aois/{aoi_id}/labels", json=body | {"changes": changes} | fields)
        assert r.status_code == 422
        return str(r.json()["detail"])

    assert "first year" in refused({"month": "2017-06"})
    assert "outside the area" in refused({"geometry": mapping(box(0, 0, 0.01, 0.01))})
    assert "go together" in refused(verified_by="me")
    assert "2019-06" in (folder / "kyiv-west.geojson").read_text()  # left as it was

    other = client.post(
        "/aois", json={"name": "Kyiv west", "geometry": mapping(box(30.6, 50.3, 30.62, 50.31))}
    ).json()
    assert client.get(f"/aois/{other['id']}/labels").status_code == 409  # never overwritten
    monkeypatch.setattr(evaluation, "EVAL_DIR", tmp_path / "missing")
    assert client.put(f"/aois/{aoi_id}/labels", json=body).status_code == 503
