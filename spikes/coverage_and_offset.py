"""Spikes 1 and 2: which Earth Search collection covers the archive, and how the
processing-baseline 04.00 reflectance offset shows up in item metadata.

Run: uv run python spikes/coverage_and_offset.py
"""

from collections import Counter, defaultdict

from pystac_client import Client

EARTH_SEARCH = "https://earth-search.aws.element84.com/v1"
COLLECTIONS = ["sentinel-2-l2a", "sentinel-2-c1-l2a", "sentinel-2-pre-c1-l2a"]
# The mockup's area (~1.9 x 1.3 km), lon/lat bbox.
AOI_BBOX = [30.674, 50.3845, 30.701, 50.396]
PERIOD = "2017-01-01/2026-11-06"

client = Client.open(EARTH_SEARCH)

for collection in COLLECTIONS:
    items = list(client.search(collections=[collection], bbox=AOI_BBOX, datetime=PERIOD).items())
    print(f"\n=== {collection}: {len(items)} items")
    if not items:
        continue

    per_year = Counter(i.datetime.year for i in items)
    print("items per year:", dict(sorted(per_year.items())))

    days = {(i.datetime.date(), i.properties.get("platform")) for i in items}
    print(
        f"distinct (day, platform) passes: {len(days)}  -> duplicates from overlapping tiles: {len(items) - len(days)}"
    )

    tiles = Counter(
        i.properties.get("s2:mgrs_tile") or i.properties.get("grid:code") for i in items
    )
    print("tiles:", dict(tiles))
    print("platforms:", dict(Counter(i.properties.get("platform") for i in items)))

    by_baseline = defaultdict(list)
    for i in items:
        by_baseline[i.properties.get("s2:processing_baseline")].append(i)
    for baseline, group in sorted(by_baseline.items(), key=lambda kv: str(kv[0])):
        group.sort(key=lambda i: i.datetime)
        sample = group[0]
        red = sample.assets.get("red")
        raster_bands = (red.extra_fields.get("raster:bands") if red else None) or [{}]
        print(
            f"  baseline {baseline}: {len(group)} items, "
            f"{group[0].datetime.date()} .. {group[-1].datetime.date()} | "
            f"boa_offset_applied={sample.properties.get('earthsearch:boa_offset_applied')} | "
            f"red raster:bands offset={raster_bands[0].get('offset')} scale={raster_bands[0].get('scale')}"
        )

    first = min(items, key=lambda i: i.datetime)
    print("asset keys:", sorted(first.assets))
    print(
        "earthsearch/s2 props:",
        sorted(k for k in first.properties if k.startswith(("earthsearch", "s2:"))),
    )
