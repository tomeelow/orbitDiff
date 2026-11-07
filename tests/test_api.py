"""API and backfill job against a real PostGIS database.

Runs when ORBITDIFF_TEST_DATABASE_URL points at a database these tests may wipe (CI sets
it; see .env.example). Imagery comes from local files, as everywhere else.
"""

import os
from datetime import UTC, datetime
from urllib.parse import parse_qs, urlsplit

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from fastapi.testclient import TestClient
from rasterio.io import MemoryFile
from shapely.geometry import box, mapping
from sqlalchemy import create_engine, text

from orbitdiff import api, catalog, db, jobs
from orbitdiff.config import REPO_ROOT, settings

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
    settings.cache_clear()
    db.engine.cache_clear()
    with migrated.begin() as c:
        c.execute(text("TRUNCATE aois RESTART IDENTITY CASCADE"))
    yield tmp_path
    db.engine().dispose()
    db.engine.cache_clear()
    settings.cache_clear()


class FakeRedis:
    def __init__(self, up: bool = True) -> None:
        self.up = up

    def ping(self) -> bool:
        if not self.up:
            raise ConnectionError("down")
        return True


@pytest.fixture
def client(data_dir):
    queued: list[int] = []
    api.app.dependency_overrides[api.get_enqueue] = lambda: queued.append
    api.app.dependency_overrides[api.get_redis] = FakeRedis
    with TestClient(api.app) as c:
        c.queued = queued
        yield c
    api.app.dependency_overrides.clear()


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
    tile_query = parse_qs(urlsplit(timeline[1]["tiles"]["true_color"]).query)
    assert tile_query["url"] == [f"/data/composites/{aoi_id}/2021-09_display.tif"]
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


def test_failed_backfill_is_recorded_and_can_be_retried(client, aoi_polygon, monkeypatch):
    def down(*args, **kwargs):
        raise RuntimeError("no catalog answered for 2017")

    monkeypatch.setattr(catalog, "search", down)
    aoi_id = create(client, aoi_polygon).json()["id"]
    with pytest.raises(RuntimeError):
        jobs.backfill(aoi_id, model=lambda *bands: None)
    progress = client.get(f"/aois/{aoi_id}/progress").json()
    assert progress["stage"] == "failed" and "no catalog answered" in progress["error"]

    assert client.post(f"/aois/{aoi_id}/backfill").status_code == 202
    assert client.post(f"/aois/{aoi_id}/backfill").status_code == 409  # already queued
    assert client.queued == [aoi_id, aoi_id]


def test_health_reports_what_is_down(client):
    assert client.get("/health").json() == {"db": "ok", "queue": "ok"}
    api.app.dependency_overrides[api.get_redis] = lambda: FakeRedis(up=False)
    r = client.get("/health")
    assert r.status_code == 503 and r.json()["queue"].startswith("down")
