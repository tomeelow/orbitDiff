"""Scene search on the imagery catalogs, and the choice of one item per pass (ADR-002).

A pass is one satellite flying over the AOI on one day. Catalogs deliver it several
times: once per MGRS tile covering the AOI (the spike AOI sits in two UTM zones), and,
for 2018-2021 on Earth Search, once more per processing version. `group_passes` keeps
every candidate for a pass, best first, so a failed read can fall back to the next one.
"""

import json
import threading
import urllib.request
from collections import defaultdict
from collections.abc import Iterable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from hashlib import sha256
from pathlib import Path

import pystac
from loguru import logger
from pystac_client import Client
from pystac_client.stac_api_io import StacApiIO
from shapely.geometry import mapping, shape
from shapely.geometry.base import BaseGeometry
from urllib3.util.retry import Retry

from orbitdiff.aoi import AOIGrid
from orbitdiff.config import Catalog
from orbitdiff.radiometry import offset_dn

PassKey = tuple[str, date]  # (platform, UTC acquisition date)


@dataclass(frozen=True)
class SceneItem:
    """One catalog item, normalized across catalogs."""

    item_id: str
    catalog: str
    acquired: datetime  # UTC
    platform: str  # sentinel-2a / 2b / 2c
    tile: str  # MGRS tile, e.g. 36UUA
    epsg: int
    baseline: str  # processing baseline, e.g. 05.11
    offset_removed: bool  # the catalog already removed the BOA offset from the pixels
    tile_cloud_pct: float  # whole tile; a loose pre-filter, never AOI usability
    footprint: BaseGeometry  # valid-data footprint, EPSG:4326
    hrefs: dict[str, str]  # our band name -> unsigned href

    @property
    def pass_key(self) -> PassKey:
        return self.platform, self.acquired.date()

    @property
    def offset_dn(self) -> int:
        return offset_dn(self.baseline, self.offset_removed)

    def to_json(self) -> dict:
        return {
            **{k: getattr(self, k) for k in ("item_id", "catalog", "platform", "tile", "epsg")},
            **{
                k: getattr(self, k)
                for k in ("baseline", "offset_removed", "tile_cloud_pct", "hrefs")
            },
            "acquired": self.acquired.isoformat(),
            "footprint": mapping(self.footprint),
        }

    @classmethod
    def from_json(cls, d: dict) -> "SceneItem":
        return cls(
            **d
            | {
                "acquired": datetime.fromisoformat(d["acquired"]),
                "footprint": shape(d["footprint"]),
            }
        )


def scene_item(item: pystac.Item, catalog: Catalog) -> SceneItem:
    p = item.properties
    tile = p.get("s2:mgrs_tile") or p["grid:code"].removeprefix("MGRS-")
    epsg = p.get("proj:epsg") or int(p["proj:code"].removeprefix("EPSG:"))
    return SceneItem(
        item_id=item.id,
        catalog=catalog.name,
        acquired=item.datetime.astimezone(UTC),
        platform=p["platform"].lower(),
        tile=tile,
        epsg=int(epsg),
        baseline=p["s2:processing_baseline"],
        offset_removed=bool(p.get("earthsearch:boa_offset_applied", False)),
        tile_cloud_pct=float(p.get("eo:cloud_cover", 100.0)),
        footprint=shape(item.geometry),
        hrefs={band: item.assets[key].href for band, key in catalog.assets.items()},
    )


@dataclass
class SearchLog:
    """What a job's searches ran into, for the admin view. Lists, because searches run in
    parallel and appending is atomic."""

    errors: list[str] = field(default_factory=list)  # "<catalog> <year>": it didn't answer
    fallbacks: list[str] = field(default_factory=list)  # "<catalog> <year>": the fallback did


def _client(catalog: Catalog) -> Client:
    retry = Retry(
        total=5, backoff_factor=1, status_forcelist=[429, 500, 502, 503, 504], allowed_methods=None
    )
    return Client.open(catalog.url, stac_io=StacApiIO(max_retries=retry))


def _search_year(catalog: Catalog, aoi: BaseGeometry, year: int) -> list[SceneItem]:
    search = _client(catalog).search(
        collections=[catalog.collection],
        intersects=mapping(aoi),
        datetime=f"{year}-01-01T00:00:00Z/{year}-12-31T23:59:59Z",
        limit=catalog.page_size,
    )
    return [scene_item(item, catalog) for item in search.items()]


def _cache_file(cache_dir: Path, catalog: Catalog, aoi: BaseGeometry, year: int) -> Path:
    key = sha256(f"{catalog.url}|{catalog.collection}|{aoi.wkt}".encode()).hexdigest()[:16]
    return cache_dir / catalog.name / key / f"{year}.json"


def search_year(
    catalogs: Sequence[Catalog],
    aoi: BaseGeometry,
    year: int,
    cache_dir: Path | None = None,
    log: SearchLog | None = None,
) -> list[SceneItem]:
    """All items over the AOI in one year, from the first catalog that answers.

    Searches for finished years are cached on disk; the current year is always re-searched.
    """
    cacheable = cache_dir is not None and year < datetime.now(UTC).year
    caches = {c.name: _cache_file(cache_dir, c, aoi, year) for c in catalogs} if cacheable else {}
    for cache in caches.values():
        if cache.exists():
            return [SceneItem.from_json(d) for d in json.loads(cache.read_text())]

    log = log if log is not None else SearchLog()
    for catalog in catalogs:
        cache = caches.get(catalog.name)
        try:
            items = _search_year(catalog, aoi, year)
        except Exception as exc:  # network, HTTP or parsing: try the next catalog
            logger.warning("search on {} failed for {}: {!r}", catalog.name, year, exc)
            log.errors.append(f"{catalog.name} {year}")
            continue
        if catalog is not catalogs[0]:
            logger.warning("used fallback catalog {} for {}", catalog.name, year)
            log.fallbacks.append(f"{catalog.name} {year}")
        if cache:
            cache.parent.mkdir(parents=True, exist_ok=True)
            cache.write_text(json.dumps([i.to_json() for i in items]))
        return items
    raise RuntimeError(f"no catalog answered for {year}")


def search(
    catalogs: Sequence[Catalog],
    aoi: BaseGeometry,
    start: date,
    end: date,
    cache_dir: Path | None = None,
    log: SearchLog | None = None,
) -> list[SceneItem]:
    """Items over the AOI between start and end (inclusive), one search per year in parallel."""
    years = list(range(start.year, end.year + 1))
    with ThreadPoolExecutor(max_workers=len(years)) as pool:
        per_year = pool.map(lambda y: search_year(catalogs, aoi, y, cache_dir, log), years)
    return [i for items in per_year for i in items if start <= i.acquired.date() <= end]


def group_passes(
    items: Iterable[SceneItem], grid: AOIGrid, primary: str
) -> dict[PassKey, list[SceneItem]]:
    """Candidates per pass, best first.

    Preference, in order: the item's footprint covers the whole AOI; it comes from the
    primary catalog; it has the newest processing baseline; it is in the AOI's own UTM
    zone, so it needs no reprojection.
    """
    passes: dict[PassKey, list[SceneItem]] = defaultdict(list)
    for item in items:
        passes[item.pass_key].append(item)

    def preference(i: SceneItem) -> tuple:
        return (
            i.footprint.contains(grid.geometry),
            i.catalog == primary,
            i.baseline,
            i.epsg == grid.epsg,
        )

    return {
        key: sorted(group, key=preference, reverse=True) for key, group in sorted(passes.items())
    }


# Planetary Computer hrefs need a SAS token; it's anonymous and lasts about an hour.
_tokens: dict[str, tuple[str, datetime]] = {}
_tokens_lock = threading.Lock()


def _token(token_url: str) -> str:
    with _tokens_lock:
        cached = _tokens.get(token_url)
        if cached and cached[1] - datetime.now(UTC) > timedelta(minutes=5):
            return cached[0]
        with urllib.request.urlopen(token_url, timeout=30) as r:
            body = json.load(r)
        expiry = datetime.fromisoformat(body["msft:expiry"].replace("Z", "+00:00"))
        _tokens[token_url] = (body["token"], expiry)
        return body["token"]


def signed_href(href: str, catalog: Catalog) -> str:
    return f"{href}?{_token(catalog.token_url)}" if catalog.token_url else href
