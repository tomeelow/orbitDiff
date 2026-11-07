# Evaluation set

The frozen set of areas every detection method is scored on (implementation plan, Phase 6).
False alerts are the product's main failure mode, so they are measured here from the start.

```bash
uv run orbitdiff evaluate --method naive --fetch   # --fetch: read and composite missing imagery
```

This prints the benchmark table (event precision and recall, false alerts per AOI-year,
median delay) with breakdowns by category and hard case. The run is logged to MLflow
(`mlflow.db`) with the set's version, the method, and every threshold in force.

## One file per area

`<name>.geojson` is a FeatureCollection:

- **One feature with `"role": "aoi"`**: the area (at most the AOI size cap), with
  - `first_month`, `last_month` (`"YYYY-MM"`): the period of imagery used. A change can
    only show from twelve months after `first_month`, once it has an anniversary to compare
    with.
  - `hard_cases`: what makes it hard, e.g. `["cropland"]`, `["floodplain"]`, `["snow"]`,
    `["deciduous_forest"]`, `["airport"]`.
  - `verified_by`, `verified_on`, `verified_with`: who checked every event, when, and against
    what imagery. A file without `verified_by` is a candidate. The table then says
    "labels NOT verified" and no result from it is a benchmark result.
- **One feature per event, with `"role": "event"`**:
  - `category`: one of `buildings_roads`, `vegetation_loss`, `solar_industry`, `water`.
  - `month`: the first month the change is visible from orbit.
  - The geometry: the changed footprint, as precise as 10 m pixels allow.

**Quiet areas** have no event features. They are the ones that only *look* like they
change: cropland, floodplains, snowy areas, deciduous forest, airports. Every alert in them
is a false alert.

## Labelling

Label in the web app, signed in as an admin (`ORBITDIFF_ADMIN_EMAILS`). The API writes
`eval/<area name>.geojson` in this checkout, in the format above; commit it like any other
change. With `docker compose`, the API mounts `./eval` for this.

1. Draw the area and let its archive backfill. Name it for its file, e.g. "Grünheide" →
   `grunheide.geojson`.
2. Open **Evaluation labels → Label this area**. While labelling, the map shows your labels
   instead of the detected changes, so the labels don't copy a detector.
3. Look through the archive (swipe A/B, NDVI). For each change, step B to the first month
   it shows, press **Draw a change**, outline it on the map, and pick its category. A
   change in the period's first twelve months can't be scored, and is refused.
4. Tick what makes the area hard, and save. The file is a candidate.
5. Verify every event, and confirm that quiet areas really are quiet, against
   high-resolution historical imagery. Use that imagery for checking only, and check its
   terms before using it for anything else.
6. Fill in who verified it, when, and against what, and save again.

Aim for 20–50 areas, events and quiet ones together.

## Freezing

The set's version is a hash of these files, logged with every metric. Edit the set
deliberately: a change to it is reported next to the metric changes it causes, never mixed
into a detector change.
