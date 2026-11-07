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

1. Pick an area, and look through its archive in the web app (swipe A/B, NDVI).
2. Draw each change's footprint and note the month it first shows.
3. Verify every event, and confirm that quiet areas really are quiet, against
   high-resolution historical imagery. Use that imagery for checking only, and check its
   terms before using it for anything else.
4. Fill in `verified_by`, `verified_on` and `verified_with`.

Aim for 20–50 areas, events and quiet ones together.

## Freezing

The set's version is a hash of these files, logged with every metric. Edit the set
deliberately: a change to it is reported next to the metric changes it causes, never mixed
into a detector change.
