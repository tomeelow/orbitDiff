import {
  GeolocateControl,
  type Map as MapLibreMap,
  NavigationControl,
  ScaleControl,
} from "maplibre-gl";
import { useEffect, useRef, useState } from "react";
import { TerraDraw, TerraDrawPolygonMode, ValidateNotSelfIntersecting } from "terra-draw";
import { TerraDrawMapLibreGLAdapter } from "terra-draw-maplibre-gl-adapter";
import { ApiError, api } from "../api";
import { navigate, type Theme, useLoad } from "../hooks";
import { type Polygon, polygonAreaKm2 } from "../lib/geo";
import { createMap, MAP_COLORS, swapBasemap } from "../map";

/** Draw one polygon, see its size against the cap, name it and start the backfill. */
export function DrawPage({ theme }: { theme: Theme }) {
  const container = useRef<HTMLDivElement>(null);
  const mapRef = useRef<MapLibreMap | null>(null);
  const drawRef = useRef<TerraDraw | null>(null);
  const [polygon, setPolygon] = useState<Polygon | null>(null);
  const [finished, setFinished] = useState(false);
  const [name, setName] = useState("");
  const [goTo, setGoTo] = useState("");
  const [goToError, setGoToError] = useState<string | null>(null);
  const [submit, setSubmit] = useState<
    { state: "idle" | "sending" } | { state: "error"; message: string }
  >({
    state: "idle",
  });
  const [limits] = useLoad(api.limits, []);

  useEffect(() => {
    document.title = "New area — Orbit Diff";
  }, []);

  // biome-ignore lint/correctness/useExhaustiveDependencies: the map is built once
  useEffect(() => {
    if (!container.current) return;
    const map = createMap(container.current, theme, null);
    map.addControl(new NavigationControl({ showCompass: false }), "top-left");
    map.addControl(
      new GeolocateControl({ positionOptions: { enableHighAccuracy: false } }),
      "top-left",
    );
    map.addControl(new ScaleControl({ unit: "metric" }), "bottom-left");
    map.on("load", () => {
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
      const polygons = () => draw.getSnapshot().filter((f) => f.geometry.type === "Polygon");
      draw.on("change", () => {
        const all = polygons();
        // One area at a time: starting a new polygon replaces the old one.
        if (all.length > 1) {
          draw.removeFeatures(all.slice(0, -1).map((f) => f.id as string));
          setFinished(false);
        }
        const latest = polygons().at(-1);
        setPolygon(latest ? (latest.geometry as Polygon) : null);
      });
      draw.on("finish", () => setFinished(true));
      drawRef.current = draw;
    });
    mapRef.current = map;
    return () => {
      drawRef.current?.stop();
      drawRef.current = null;
      map.remove();
      mapRef.current = null;
    };
  }, []);

  const firstTheme = useRef(theme);
  useEffect(() => {
    if (!mapRef.current || theme === firstTheme.current) return;
    firstTheme.current = theme;
    swapBasemap(mapRef.current, theme);
  }, [theme]);

  const area = polygon ? polygonAreaKm2(polygon) : 0;
  const cap = limits.state === "done" ? limits.data.max_area_km2 : null;
  const tooLarge = cap !== null && area > cap;
  const ready = finished && polygon !== null && !tooLarge && name.trim().length > 0;
  const hint = !finished
    ? "Close the shape to continue."
    : !tooLarge && !name.trim()
      ? "Name the area to continue."
      : null;

  function clear() {
    drawRef.current?.clear();
    drawRef.current?.setMode("polygon");
    setPolygon(null);
    setFinished(false);
    setSubmit({ state: "idle" });
  }

  function jump(event: React.FormEvent) {
    event.preventDefault();
    const match = /^\s*(-?\d+(?:\.\d+)?)\s*[,\s]\s*(-?\d+(?:\.\d+)?)\s*$/.exec(goTo);
    const lat = Number(match?.[1]);
    const lon = Number(match?.[2]);
    if (!match || Math.abs(lat) > 90 || Math.abs(lon) > 180) {
      setGoToError("Enter latitude and longitude, like 50.39, 30.69");
      return;
    }
    setGoToError(null);
    mapRef.current?.flyTo({ center: [lon, lat], zoom: 13 });
  }

  async function start(event: React.FormEvent) {
    event.preventDefault();
    if (!ready || !polygon) return;
    setSubmit({ state: "sending" });
    try {
      const created = await api.createArea(name.trim(), polygon);
      navigate(`#/areas/${created.id}`);
    } catch (error) {
      setSubmit({
        state: "error",
        message: error instanceof ApiError ? error.message : "The area could not be created.",
      });
    }
  }

  return (
    <main className="screen draw">
      <div className="stage">
        <div className="draw-map" ref={container} />
      </div>
      <aside className="panel">
        <section className="card" aria-labelledby="draw-title">
          <h1 id="draw-title">New area</h1>
          <p className="muted">
            Click on the map to place corners. Click the first corner to close the shape, then drag
            corners to adjust. Starting a new shape replaces the old one.
          </p>
          <form className="goto" onSubmit={jump}>
            <label htmlFor="goto">Go to coordinates</label>
            <div className="field-row">
              <input
                id="goto"
                className="input"
                inputMode="decimal"
                placeholder="50.39, 30.69"
                value={goTo}
                onChange={(e) => setGoTo(e.target.value)}
                aria-describedby={goToError ? "goto-error" : undefined}
              />
              <button type="submit" className="btn">
                Go
              </button>
            </div>
            {goToError && (
              <p id="goto-error" className="error-text small">
                {goToError}
              </p>
            )}
          </form>
        </section>

        <form className="card" onSubmit={start} aria-labelledby="size-title">
          <h2 id="size-title">Size</h2>
          <p className="area-figure">
            <span className="fig">{area.toFixed(2)}</span> km²
            {cap !== null && (
              <span className="muted">
                {" "}
                of <span className="fig">{cap}</span> km² allowed
              </span>
            )}
          </p>
          {cap !== null && (
            <div className="meter" data-over={tooLarge || undefined}>
              <span style={{ width: `${Math.min(100, (area / cap) * 100)}%` }} />
            </div>
          )}
          {limits.state === "error" && <p className="error-text small">{limits.message}</p>}
          {tooLarge && (
            <p className="error-text small" role="alert">
              Too large. Orbit Diff reads only the area's window of each pass, which works for areas
              up to {cap} km². Draw a smaller one.
            </p>
          )}
          <label htmlFor="area-name">Name</label>
          <input
            id="area-name"
            className="input"
            maxLength={80}
            placeholder="Kyiv west"
            value={name}
            onChange={(e) => setName(e.target.value)}
          />
          {submit.state === "error" && (
            <p className="error-text small" role="alert">
              {submit.message}
            </p>
          )}
          <div className="actions">
            <button
              type="submit"
              className="btn btn-primary"
              disabled={!ready || submit.state === "sending"}
            >
              {submit.state === "sending" ? "Starting…" : "Build the archive"}
            </button>
            <button type="button" className="btn" onClick={clear} disabled={!polygon}>
              Clear
            </button>
          </div>
          {hint && <p className="muted small">{hint}</p>}
        </form>
      </aside>
    </main>
  );
}
