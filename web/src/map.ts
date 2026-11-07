/** MapLibre setup shared by the draw and archive screens. */

import {
  type ImageSource,
  Map as MapLibreMap,
  type MapOptions,
  type RasterTileSource,
  type StyleSpecification,
  setWorkerUrl,
} from "maplibre-gl";
import workerUrl from "maplibre-gl/dist/maplibre-gl-worker.mjs?url";
import type { Theme } from "./hooks";
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
