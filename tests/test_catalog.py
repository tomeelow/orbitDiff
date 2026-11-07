from dataclasses import replace
from datetime import UTC, date, datetime

import pystac
import pytest
from shapely.geometry import box, mapping

from orbitdiff import catalog
from orbitdiff.config import REPO_ROOT, load_pipeline_config

CFG = load_pipeline_config(REPO_ROOT / "config" / "pipeline.toml")
EARTH_SEARCH, PLANETARY = CFG.catalog.primary, CFG.catalog.fallback


def stac_item(item_id: str, props: dict, asset_keys) -> pystac.Item:
    item = pystac.Item(
        id=item_id,
        geometry=mapping(box(29.5, 49.9, 31.1, 51.0)),
        bbox=[29.5, 49.9, 31.1, 51.0],
        datetime=datetime(2021, 8, 15, 8, 58, 45, tzinfo=UTC),
        properties=props,
    )
    for key in asset_keys:
        item.add_asset(key, pystac.Asset(href=f"https://example.test/{item_id}/{key}.tif"))
    return item


def test_earth_search_item_is_normalized():
    props = {
        "platform": "sentinel-2b",
        "grid:code": "MGRS-36UUA",
        "proj:code": "EPSG:32636",
        "s2:processing_baseline": "05.00",
        "earthsearch:boa_offset_applied": True,
        "eo:cloud_cover": 3.5,
    }
    s = catalog.scene_item(stac_item("es", props, EARTH_SEARCH.assets.values()), EARTH_SEARCH)
    assert (s.platform, s.tile, s.epsg, s.offset_dn) == ("sentinel-2b", "36UUA", 32636, 0)
    assert s.hrefs["nir"].endswith("/nir.tif")
    assert s.pass_key == ("sentinel-2b", date(2021, 8, 15))


def test_planetary_computer_item_is_normalized():
    props = {
        "platform": "Sentinel-2B",
        "s2:mgrs_tile": "36UUA",
        "proj:code": "EPSG:32636",
        "s2:processing_baseline": "05.00",
        "eo:cloud_cover": 3.5,
    }
    s = catalog.scene_item(stac_item("pc", props, PLANETARY.assets.values()), PLANETARY)
    # Same pass key as Earth Search's item, and the offset is still in PC's pixels.
    assert s.pass_key == ("sentinel-2b", date(2021, 8, 15))
    assert s.offset_dn == -1000
    assert s.hrefs["nir"].endswith("/B08.tif")


def test_scene_item_round_trips_through_json(make_scene):
    s = make_scene()
    assert catalog.SceneItem.from_json(s.to_json()) == s


def test_one_pass_prefers_coverage_then_primary_then_baseline_then_own_zone(
    make_scene, grid, aoi_polygon
):
    base = make_scene()
    other_zone = replace(base, item_id="35UQR_reprocessed", epsg=32635, tile="35UQR")
    original = replace(base, item_id="36UUA_original", baseline="03.01")
    partial = replace(
        base, item_id="36UUA_partial", footprint=box(30.0, 50.0, 30.68, 51.0), baseline="05.11"
    )
    fallback = replace(base, item_id="pc", catalog="planetary-computer", baseline="05.11")
    next_pass = replace(base, item_id="next", acquired=datetime(2021, 8, 18, 9, tzinfo=UTC))

    passes = catalog.group_passes(
        [partial, original, fallback, other_zone, base, next_pass], grid, primary="earth-search"
    )
    assert list(passes) == [("sentinel-2b", date(2021, 8, 15)), ("sentinel-2b", date(2021, 8, 18))]
    ranked = [s.item_id for s in passes[("sentinel-2b", date(2021, 8, 15))]]
    assert ranked == [base.item_id, "35UQR_reprocessed", "36UUA_original", "pc", "36UUA_partial"]


def test_search_falls_back_and_caches_finished_years(
    monkeypatch, tmp_path, make_scene, aoi_polygon
):
    calls = []

    def fake_search(cat, aoi, year):
        calls.append((cat.name, year))
        if cat.name == "earth-search":
            raise ConnectionError("down")
        return [make_scene()]

    monkeypatch.setattr(catalog, "_search_year", fake_search)
    catalogs = [EARTH_SEARCH, PLANETARY]

    first = catalog.search_year(catalogs, aoi_polygon, 2021, cache_dir=tmp_path)
    again = catalog.search_year(catalogs, aoi_polygon, 2021, cache_dir=tmp_path)
    assert first == again
    assert calls == [
        ("earth-search", 2021),
        ("planetary-computer", 2021),
    ]  # second call hit the cache


def test_search_never_caches_the_current_year(monkeypatch, tmp_path, make_scene, aoi_polygon):
    monkeypatch.setattr(catalog, "_search_year", lambda cat, aoi, year: [make_scene()])
    catalog.search_year([EARTH_SEARCH], aoi_polygon, datetime.now(UTC).year, cache_dir=tmp_path)
    assert not list(tmp_path.rglob("*.json"))


def test_search_filters_to_the_period(monkeypatch, make_scene, aoi_polygon):
    scenes = [make_scene(f"s{m}", acquired=datetime(2021, m, 10, tzinfo=UTC)) for m in (1, 6, 12)]
    monkeypatch.setattr(catalog, "_search_year", lambda cat, aoi, year: scenes)
    found = catalog.search([EARTH_SEARCH], aoi_polygon, date(2021, 3, 1), date(2021, 11, 30))
    assert [s.item_id for s in found] == ["s6"]


def test_no_catalog_answering_is_an_error(monkeypatch, aoi_polygon):
    def down(cat, aoi, year):
        raise ConnectionError("down")

    monkeypatch.setattr(catalog, "_search_year", down)
    with pytest.raises(RuntimeError):
        catalog.search_year([EARTH_SEARCH, PLANETARY], aoi_polygon, 2021)


def test_planetary_computer_hrefs_are_signed_and_tokens_reused(monkeypatch):
    fetched = []

    def fake_token(url):
        fetched.append(url)
        return "st=1&sig=abc"

    monkeypatch.setattr(catalog, "_token", fake_token)
    assert catalog.signed_href("https://b/x.tif", PLANETARY) == "https://b/x.tif?st=1&sig=abc"
    assert catalog.signed_href("https://e/x.tif", EARTH_SEARCH) == "https://e/x.tif"
    assert fetched == [PLANETARY.token_url]
