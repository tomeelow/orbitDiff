import { type Map as MapLibreMap, NavigationControl, ScaleControl } from "maplibre-gl";
import { useEffect, useMemo, useRef, useState } from "react";
import {
  type AoiDetail,
  type ChangeEvent,
  type Month,
  pointReadout,
  type Scene,
  sceneImageUrl,
} from "../api";
import type { Theme } from "../hooks";
import { formatLonLat, formatUtc } from "../lib/format";
import { type Bounds, boundsOf, type LonLat } from "../lib/geo";
import { monthKey, monthLabel } from "../lib/months";
import { intersect, tilesCovering, tileUrl } from "../lib/tiles";
import {
  addOutline,
  createMap,
  setChanges,
  setPassImage,
  setRaster,
  swapBasemap,
  whenStyleReady,
} from "../map";

export type Layer = "true_color" | "ndvi";

interface Props {
  area: AoiDetail;
  a: Month | null;
  b: Month | null;
  layer: Layer;
  pass: Scene | null; // B shows this pass instead of the monthly composite
  next: Month | null; // the timelapse's next frame, preloaded
  theme: Theme;
  changes?: ChangeEvent[]; // footprints drawn on both months
  selected?: number | null; // the change in focus: drawn filled, and the map fits it
  onPick?: (id: number) => void; // a footprint was clicked
}

interface Readout {
  lonLat: [number, number];
  side: "A" | "B";
  month: string;
  value: { ndvi: number | null; filled: boolean } | null | "loading" | "pass";
}

/** Month A on the left of the divider, month B on the right: two maps, one camera. */
export function CompareMap({
  area,
  a,
  b,
  layer,
  pass,
  next,
  theme,
  changes = [],
  selected = null,
  onPick,
}: Props) {
  const aRef = useRef<HTMLDivElement>(null);
  const bRef = useRef<HTMLDivElement>(null);
  const frameRef = useRef<HTMLDivElement>(null);
  const maps = useRef<{ a: MapLibreMap; b: MapLibreMap } | null>(null);
  const [ready, setReady] = useState(false);
  const [split, setSplit] = useState(50);
  const [readout, setReadout] = useState<Readout | null>(null);
  const [tileError, setTileError] = useState(false);
  const bounds: Bounds = useMemo(() => boundsOf(area.corners), [area.corners]);
  const compare = a !== null;

  // What the hover handler needs, without re-binding it every render.
  const live = useRef({ a, b, pass, split, compare, onPick });
  live.current = { a, b, pass, split, compare, onPick };

  // biome-ignore lint/correctness/useExhaustiveDependencies: the maps are built once per area
  useEffect(() => {
    if (!aRef.current || !bRef.current) return;
    const mapB = createMap(bRef.current, theme, bounds);
    const mapA = createMap(aRef.current, theme, bounds, {
      interactive: false,
      attributionControl: false,
    });
    mapB.addControl(new NavigationControl({ showCompass: false }), "top-left");
    mapB.addControl(new ScaleControl({ unit: "metric" }), "bottom-left");
    mapB.on("move", () =>
      mapA.jumpTo({ center: mapB.getCenter(), zoom: mapB.getZoom(), bearing: mapB.getBearing() }),
    );
    mapB.on("error", (event) => {
      if ("sourceId" in event && String(event.sourceId).startsWith("od-")) setTileError(true);
    });
    let loaded = 0;
    for (const map of [mapA, mapB]) {
      map.on("load", () => {
        addOutline(map, area.geometry);
        loaded += 1;
        if (loaded === 2) setReady(true);
      });
    }
    maps.current = { a: mapA, b: mapB };

    let timer = 0;
    let request: AbortController | null = null;
    mapB.on("mousemove", (event) => {
      const { a, b, pass, split, compare } = live.current;
      const width = mapB.getContainer().clientWidth;
      const side = compare && event.point.x < (split / 100) * width ? "A" : "B";
      const month = side === "A" ? a : b;
      if (!month) return;
      const lonLat: [number, number] = [event.lngLat.lng, event.lngLat.lat];
      const key = monthKey(month.month);
      if (side === "B" && pass) {
        setReadout({ lonLat, side, month: key, value: "pass" });
        return;
      }
      setReadout({ lonLat, side, month: key, value: "loading" });
      window.clearTimeout(timer);
      request?.abort();
      timer = window.setTimeout(() => {
        request = new AbortController();
        pointReadout(month.tiles.point, lonLat, request.signal).then(
          (value) => setReadout({ lonLat, side, month: key, value }),
          () => {}, // aborted by the next move
        );
      }, 120);
    });
    mapB.on("click", "od-changes-fill", (event) => {
      const id = event.features?.[0]?.properties?.id;
      if (typeof id === "number") live.current.onPick?.(id);
    });
    mapB.on("mouseenter", "od-changes-fill", () => {
      mapB.getCanvas().style.cursor = "pointer";
    });
    mapB.on("mouseleave", "od-changes-fill", () => {
      mapB.getCanvas().style.cursor = "";
    });
    mapB.on("mouseout", () => {
      window.clearTimeout(timer);
      request?.abort();
      setReadout(null);
    });

    return () => {
      window.clearTimeout(timer);
      request?.abort();
      mapA.remove();
      mapB.remove();
      maps.current = null;
      setReady(false);
    };
  }, [area.id]);

  const firstTheme = useRef(theme);
  useEffect(() => {
    if (!ready || !maps.current || theme === firstTheme.current) return;
    firstTheme.current = theme;
    swapBasemap(maps.current.a, theme);
    swapBasemap(maps.current.b, theme);
  }, [theme, ready]);

  const aUrl = a?.tiles[layer] ?? null;
  const bUrl = b?.tiles[layer] ?? null;
  const passUrl = pass?.images ? sceneImageUrl(pass.images[layer]) : null;

  useEffect(() => {
    const current = maps.current;
    if (!ready || !current) return;
    setTileError(false);
    whenStyleReady(current.a, () => setRaster(current.a, aUrl, bounds));
    whenStyleReady(current.b, () => {
      setRaster(current.b, passUrl ? null : bUrl, bounds);
      setPassImage(current.b, passUrl, area.corners);
    });
  }, [ready, aUrl, bUrl, passUrl, area.corners, bounds]);

  useEffect(() => {
    const current = maps.current;
    if (!ready || !current) return;
    for (const map of [current.a, current.b])
      whenStyleReady(map, () => setChanges(map, changes, selected));
  }, [ready, changes, selected]);

  // Bring the selected change into view, with its surroundings.
  const focus = changes.find((e) => e.id === selected)?.geometry;
  useEffect(() => {
    const map = maps.current?.b;
    if (!ready || !map || !focus) return;
    const rings = focus.type === "Polygon" ? [focus.coordinates] : focus.coordinates;
    const points = rings.flatMap((polygon) => polygon.flat() as LonLat[]);
    map.fitBounds(boundsOf(points), { padding: 96, maxZoom: 15, duration: 600 });
  }, [ready, focus]);

  // Timelapse: fetch the next frame's tiles for the visible part of the area. Tile URLs are
  // versioned and served immutable, so the map then reads them from the browser cache.
  useEffect(() => {
    const map = maps.current?.b;
    const template = next?.tiles[layer];
    if (!ready || !map || !template) return;
    const view = map.getBounds();
    const visible = intersect(bounds, [
      view.getWest(),
      view.getSouth(),
      view.getEast(),
      view.getNorth(),
    ]);
    if (!visible) return;
    const z = Math.max(0, Math.min(22, Math.round(map.getZoom())));
    const controller = new AbortController();
    for (const t of tilesCovering(visible, z).slice(0, 16)) {
      fetch(tileUrl(template, t), { signal: controller.signal, mode: "cors" }).catch(() => {});
    }
    return () => controller.abort();
  }, [next, layer, ready, bounds]);

  function dragTo(clientX: number) {
    const frame = frameRef.current?.getBoundingClientRect();
    if (!frame) return;
    setSplit(Math.min(100, Math.max(0, ((clientX - frame.left) / frame.width) * 100)));
  }

  function onKnobKey(event: React.KeyboardEvent) {
    const step = event.shiftKey ? 20 : 5;
    const moves: Record<string, number> = {
      ArrowLeft: -step,
      ArrowRight: step,
      Home: -100,
      End: 100,
    };
    const move = moves[event.key];
    if (move === undefined) return;
    event.preventDefault();
    setSplit((s) => Math.min(100, Math.max(0, s + move)));
  }

  const bLabel = pass
    ? `${formatUtc(pass.acquired_utc)} UTC`
    : b
      ? monthLabel(monthKey(b.month))
      : null;

  return (
    <div className="compare" ref={frameRef}>
      <div className="compare-map" ref={bRef} />
      <div
        className="compare-map compare-a"
        ref={aRef}
        hidden={!compare}
        style={{ clipPath: `inset(0 ${100 - split}% 0 0)` }}
        aria-hidden="true"
      />
      {compare && a && (
        <span className="map-tag map-tag-a">
          <b>A</b>
          {monthLabel(monthKey(a.month))}
        </span>
      )}
      {bLabel && (
        <span className="map-tag map-tag-b">
          <b>B</b>
          {bLabel}
        </span>
      )}
      {compare && (
        <div
          className="divider"
          style={{ left: `${split}%` }}
          onPointerDown={(event) => {
            event.currentTarget.setPointerCapture(event.pointerId);
            dragTo(event.clientX);
          }}
          onPointerMove={(event) => {
            if (event.currentTarget.hasPointerCapture(event.pointerId)) dragTo(event.clientX);
          }}
        >
          <div
            className="divider-knob"
            role="slider"
            tabIndex={0}
            aria-label="Divider between month A and month B"
            aria-valuemin={0}
            aria-valuemax={100}
            aria-valuenow={Math.round(split)}
            aria-valuetext={`${Math.round(split)}% of the map shows month A`}
            onKeyDown={onKnobKey}
          />
        </div>
      )}
      {readout && <ReadoutLine readout={readout} />}
      {tileError && (
        <p className="map-error" role="alert">
          Imagery tiles failed to load. Is TiTiler running on port 8001?
        </p>
      )}
    </div>
  );
}

function ReadoutLine({ readout }: { readout: Readout }) {
  const { value } = readout;
  let text: string;
  if (value === "loading") text = "NDVI …";
  else if (value === "pass") text = "NDVI readout is for monthly composites";
  else if (!value || value.ndvi === null) text = "no data here";
  else text = `NDVI ${value.ndvi.toFixed(2)}${value.filled ? ", filled for display" : ""}`;
  return (
    <output className="readout">
      <span className="fig">{formatLonLat(...readout.lonLat)}</span>
      <span>
        <b>{readout.side}</b> {monthLabel(readout.month)}
      </span>
      <span className={typeof value === "object" && value?.filled ? "readout-filled" : undefined}>
        {text}
      </span>
    </output>
  );
}
