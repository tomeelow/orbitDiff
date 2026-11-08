/** MapLibre setup shared by the draw and archive screens. */

import {
  type ExpressionSpecification,
  type GeoJSONSource,
  type ImageSource,
  Map as MapLibreMap,
  type MapOptions,
  type RasterTileSource,
  type StyleSpecification,
  setWorkerUrl,
} from "maplibre-gl";
import workerUrl from "maplibre-gl/dist/maplibre-gl-worker.mjs?url";
import { TerraDraw, TerraDrawPolygonMode, ValidateNotSelfIntersecting } from "terra-draw";
import { TerraDrawMapLibreGLAdapter } from "terra-draw-maplibre-gl-adapter";
import { API_URL, type ChangeEvent, type LabelChange } from "./api";
import type { Theme } from "./hooks";
import { CATEGORY } from "./lib/changes";
import type { Bounds, Polygon } from "./lib/geo";

// MapLibre finds its worker next to its own module, which bundling moves; say where it is.
setWorkerUrl(workerUrl);

/** Basemaps under the imagery: OpenStreetMap data from OpenFreeMap, no key. */
export const BASEMAP: Record<Theme, string> = {
  dark: import.meta.env.VITE_BASEMAP_DARK ?? "https://tiles.openfreemap.org/styles/dark",
  light: import.meta.env.VITE_BASEMAP_LIGHT ?? "https://tiles.openfreemap.org/styles/positron",
};

/** MapLibre and Terra Draw take hex colours: Rim's accent and the outline over imagery. */
export const MAP_COLORS = {
  accent: "#f97c3d", // --color-accent, dark
  outline: "#f6f3ee", // --color-on-map
  shade: "#0b0a08", // --color-map-shade without its alpha
} as const;

/** Our sources and layers ("od-") and Terra Draw's ("td") survive a basemap swap. */
const ours = (id: string) => id.startsWith("od-") || id.startsWith("td");

export function createMap(
  container: HTMLElement,
  theme: Theme,
  bounds: Bounds | null,
  options: Partial<MapOptions> = {},
): MapLibreMap {
  return new MapLibreMap({
    container,
    style: BASEMAP[theme],
    ...(bounds ? { bounds, fitBoundsOptions: { padding: 48 } } : { center: [15, 50], zoom: 3 }),
    attributionControl: { compact: true },
    dragRotate: false,
    pitchWithRotate: false,
    // Imagery needs the session cookie, and only imagery gets it: not the basemap's host.
    transformRequest: (url) =>
      url.startsWith(API_URL) ? { url, credentials: "include" } : { url },
    ...options,
  });
}

/** Maps whose basemap is being swapped: sources can't be added until the new style loads. */
const swapping = new WeakSet<MapLibreMap>();

/** Run `update` now, or once a basemap swap in progress has loaded. */
export function whenStyleReady(map: MapLibreMap, update: () => void): void {
  if (swapping.has(map)) map.once("style.load", update);
  else update();
}

/** Switch the basemap for a theme, keeping the imagery, outline and drawing on top. */
export function swapBasemap(map: MapLibreMap, theme: Theme): void {
  swapping.add(map);
  map.once("style.load", () => swapping.delete(map));
  map.setStyle(BASEMAP[theme], {
    diff: false,
    transformStyle: (previous: StyleSpecification | undefined, next: StyleSpecification) => {
      if (!previous) return next;
      const sources = Object.fromEntries(
        Object.entries(previous.sources).filter(([id]) => ours(id)),
      );
      return {
        ...next,
        sources: { ...next.sources, ...sources },
        layers: [...next.layers, ...previous.layers.filter((l) => ours(l.id))],
      };
    },
  });
}

/** Add the AOI outline (a light line on a dark casing, legible on any imagery). */
export function addOutline(map: MapLibreMap, geometry: Polygon): void {
  if (map.getSource("od-aoi")) return;
  map.addSource("od-aoi", { type: "geojson", data: { type: "Feature", geometry, properties: {} } });
  map.addLayer({
    id: "od-aoi-casing",
    type: "line",
    source: "od-aoi",
    paint: { "line-color": MAP_COLORS.shade, "line-width": 3.5, "line-opacity": 0.6 },
  });
  map.addLayer({
    id: "od-aoi-line",
    type: "line",
    source: "od-aoi",
    paint: { "line-color": MAP_COLORS.outline, "line-width": 1.5 },
  });
}

/** Show a raster tile template under the outline, creating the layer the first time. */
export function setRaster(map: MapLibreMap, template: string | null, bounds: Bounds): void {
  const source = map.getSource<RasterTileSource>("od-composite");
  if (!template) {
    if (map.getLayer("od-composite")) map.setLayoutProperty("od-composite", "visibility", "none");
    return;
  }
  if (source) {
    source.setTiles([template]);
  } else {
    map.addSource("od-composite", { type: "raster", tiles: [template], tileSize: 512, bounds });
    map.addLayer(
      {
        id: "od-composite",
        type: "raster",
        source: "od-composite",
        // 10 m pixels shown as pixels: smoothing would invent detail that was never measured.
        paint: { "raster-resampling": "nearest", "raster-fade-duration": 0 },
      },
      "od-aoi-casing",
    );
  }
  map.setLayoutProperty("od-composite", "visibility", "visible");
}

/** One pass as an image placed on the AOI grid's corners, or none. */
export function setPassImage(
  map: MapLibreMap,
  url: string | null,
  corners: [[number, number], [number, number], [number, number], [number, number]],
): void {
  const source = map.getSource<ImageSource>("od-pass");
  if (!url) {
    if (map.getLayer("od-pass")) map.removeLayer("od-pass");
    if (source) map.removeSource("od-pass");
    return;
  }
  if (source) {
    source.updateImage({ url, coordinates: corners });
    return;
  }
  map.addSource("od-pass", { type: "image", url, coordinates: corners });
  map.addLayer(
    {
      id: "od-pass",
      type: "raster",
      source: "od-pass",
      paint: { "raster-resampling": "nearest", "raster-fade-duration": 0 },
    },
    "od-aoi-casing",
  );
}

/** Change footprints in their category's colour: pending dashed, dismissed and seasonal
dimmed, the selected one filled. The fill layer is also the click target. */
export function setChanges(map: MapLibreMap, events: ChangeEvent[], selected: number | null): void {
  const data: GeoJSON.FeatureCollection = {
    type: "FeatureCollection",
    features: events.map((e) => ({
      type: "Feature",
      id: e.id,
      geometry: e.geometry,
      properties: {
        id: e.id,
        color: CATEGORY[e.category].color,
        pending: e.status === "pending",
        dim: e.status === "dismissed" || e.status === "seasonal",
        selected: e.id === selected,
      },
    })),
  };
  const source = map.getSource<GeoJSONSource>("od-changes");
  if (source) {
    source.setData(data);
    return;
  }
  map.addSource("od-changes", { type: "geojson", data });
  const width = (base: number): ExpressionSpecification => [
    "case",
    ["get", "selected"],
    base + 1.5,
    base,
  ];
  const opacity: ExpressionSpecification = ["case", ["get", "dim"], 0.5, 1];
  map.addLayer(
    {
      id: "od-changes-fill",
      type: "fill",
      source: "od-changes",
      paint: {
        "fill-color": ["get", "color"],
        "fill-opacity": ["case", ["get", "selected"], 0.28, 0],
      },
    },
    "od-aoi-casing",
  );
  map.addLayer(
    {
      id: "od-changes-casing",
      type: "line",
      source: "od-changes",
      paint: { "line-color": MAP_COLORS.shade, "line-width": width(3), "line-opacity": 0.55 },
    },
    "od-aoi-casing",
  );
  for (const pending of [false, true]) {
    map.addLayer(
      {
        id: pending ? "od-changes-dash" : "od-changes-line",
        type: "line",
        source: "od-changes",
        filter: ["==", ["get", "pending"], pending],
        paint: {
          "line-color": ["get", "color"],
          "line-width": width(1.5),
          "line-opacity": opacity,
          ...(pending ? { "line-dasharray": [2, 1.5] } : {}),
        },
      },
      "od-aoi-casing",
    );
  }
}

/** Evaluation labels: solid outlines in their category's colour over a light fill. The map
hides detected changes while labelling, so the two never share it. */
export function setLabels(map: MapLibreMap, changes: LabelChange[]): void {
  const data: GeoJSON.FeatureCollection = {
    type: "FeatureCollection",
    features: changes.map((change) => ({
      type: "Feature",
      geometry: change.geometry,
      properties: { color: CATEGORY[change.category].color },
    })),
  };
  const source = map.getSource<GeoJSONSource>("od-labels");
  if (source) {
    source.setData(data);
    return;
  }
  map.addSource("od-labels", { type: "geojson", data });
  map.addLayer(
    {
      id: "od-labels-fill",
      type: "fill",
      source: "od-labels",
      paint: { "fill-color": ["get", "color"], "fill-opacity": 0.18 },
    },
    "od-aoi-casing",
  );
  map.addLayer(
    {
      id: "od-labels-casing",
      type: "line",
      source: "od-labels",
      paint: { "line-color": MAP_COLORS.shade, "line-width": 4, "line-opacity": 0.55 },
    },
    "od-aoi-casing",
  );
  map.addLayer(
    {
      id: "od-labels-line",
      type: "line",
      source: "od-labels",
      paint: { "line-color": ["get", "color"], "line-width": 2 },
    },
    "od-aoi-casing",
  );
}

/** Terra Draw on `map`, drawing polygons that may not cross themselves. Stop it to remove it. */
export function drawPolygons(map: MapLibreMap): TerraDraw {
  const draw = new TerraDraw({
    adapter: new TerraDrawMapLibreGLAdapter({ map }),
    modes: [
      new TerraDrawPolygonMode({
        editable: true,
        validation: (feature) => ValidateNotSelfIntersecting(feature),
        styles: {
          fillColor: MAP_COLORS.accent,
          fillOpacity: 0.12,
          outlineColor: MAP_COLORS.accent,
          outlineWidth: 2,
          closingPointColor: MAP_COLORS.accent,
          closingPointOutlineColor: MAP_COLORS.outline,
          closingPointWidth: 5,
          closingPointOutlineWidth: 2,
          editedPointColor: MAP_COLORS.accent,
          coordinatePointColor: MAP_COLORS.accent,
        },
      }),
    ],
  });
  draw.start();
  draw.setMode("polygon");
  return draw;
}
