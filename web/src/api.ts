/** The Orbit Diff API, typed as it answers (src/orbitdiff/api.py). */

import type { LonLat, Polygon } from "./lib/geo";

/** Absolute even when built with a path like "/api": MapLibre needs whole URLs. */
export const API_URL: string = new URL(
  import.meta.env.VITE_API_URL ?? "http://localhost:8000",
  globalThis.location?.href,
).href.replace(/\/$/, "");

export type Stage =
  | "queued"
  | "reading"
  | "masking"
  | "compositing"
  | "detecting"
  | "ready"
  | "failed";

export interface AoiSummary {
  id: number;
  name: string;
  area_ha: number;
  stage: Stage;
  alerts_on: boolean;
}

export interface AoiDetail extends AoiSummary {
  geometry: Polygon;
  corners: [LonLat, LonLat, LonLat, LonLat]; // the AOI grid: TL, TR, BR, BL
  archive_start: string;
  first_month: string | null;
  last_month: string | null;
  scenes: number; // passes indexed
  clear_scenes: number; // passes with enough of the AOI clear to count
  bytes_read: number;
  whole_files_bytes: number;
}

export interface Progress {
  stage: Stage;
  done: number;
  total: number;
  error: string | null;
}

export interface Month {
  month: string;
  passes: number;
  with_bands: number;
  clear_scenes: number;
  observed_frac: number;
  filled_frac: number;
  gap: boolean;
  tiles: { true_color: string; ndvi: string; point: string }; // absolute, with placeholders
}

export interface NdviPoint {
  month: string;
  ndvi: number | null;
  observed_frac: number;
}

export type ReadStatus = "full" | "scl" | "tile_cloud";

export interface Scene {
  item_id: string;
  acquired_utc: string;
  platform: string;
  catalog: string;
  read_status: ReadStatus;
  aoi_clear_frac: number | null;
  offset_suspect: boolean;
  images: { true_color: string; ndvi: string } | null; // paths on the API
}

export type Category = "buildings_roads" | "vegetation_loss" | "solar_industry" | "water" | "other";

export type EventStatus = "pending" | "confirmed" | "dismissed" | "seasonal";

export interface StatusChange {
  status: EventStatus;
  cause: string; // the pass (STAC item id) or month that caused it
  at: string; // UTC
}

/** One change, as the pipeline found it and followed it pass by pass. */
export interface ChangeEvent {
  id: number;
  month: string; // the composite month it first showed in
  reference_month: string; // the same month a year earlier
  category: Category;
  status: EventStatus;
  area_ha: number;
  geometry: Polygon | { type: "MultiPolygon"; coordinates: LonLat[][][] };
  ndvi_before: number | null;
  ndvi_after: number | null;
  confidence: number | null; // share of it that changed on the passes that decided it
  detected_utc: string;
  status_utc: string;
  history: StatusChange[];
  items: string[];
  method_version: string;
  alerts: boolean; // would have alerted at some point under the area's rule, decided by the API
  alert_reason: string;
  not_real: boolean; // the user said it isn't a real change
}

/** What an area's alert rule lets through. Only the API evaluates it. */
export interface AlertSettings {
  enabled: boolean;
  categories: Category[];
  skip_seasonal: boolean;
  min_ha: number;
}

export interface AlertRule extends AlertSettings {
  watching_since: string | null; // only passes acquired after this alert
  min_ha_range: [number, number];
}

export interface RulePreview {
  changes: number; // past changes in the area
  would_alert: number; // of them, those these settings would have emailed about
  retracted: number; // of those, later dismissed or found seasonal
}

/** Evaluation labels for an area (admins): eval/<name>.geojson, eval/README.md. */
export type LabelCategory = Exclude<Category, "other">;

export interface LabelChange {
  category: LabelCategory;
  month: string; // "2021-05": the first month it's visible from orbit
  geometry: Polygon | { type: "MultiPolygon"; coordinates: LonLat[][][] };
}

export interface LabelSet {
  first_month: string;
  last_month: string;
  hard_cases: string[];
  verified_by: string | null;
  verified_on: string | null; // "2026-11-07"
  verified_with: string | null;
  changes: LabelChange[];
}

export interface Labels extends LabelSet {
  name: string; // saved as eval/<name>.geojson
  saved: boolean;
}

export interface Me {
  email: string;
  admin: boolean; // sees the admin view
}

/** The admin view: aggregate only, no names, places or addresses (monitoring.py). */
export interface AdminSummary {
  production_version: string;
  precision_days: number;
  health_days: number;
  min_precision: number;
  min_alerts: number; // fewer alerts than this: too few to flag
  precision: {
    method_version: string;
    category: Category;
    alerts: number;
    not_real: number;
    precision: number;
    flagged: boolean;
  }[];
  health: {
    runs: number;
    failed_runs: number;
    passes_read: number;
    passes_failed: number;
    fallback_passes: number;
    read_retries: number;
    search_errors: number;
    search_fallbacks: number;
    bytes_read: number;
    median_run_minutes: number | null;
    emails_sent: number;
    emails_failed: number;
    emails_stuck: number;
    median_alert_hours: number | null;
    feedback: number;
  };
  failures: { aoi_id: number; finished_utc: string; error: string }[];
  evaluation: {
    eval_set: string; // a hash of the label files
    verified: boolean; // false: these numbers are not a benchmark
    scored_utc: string;
    rows: {
      method: string;
      precision: number | null;
      recall: number | null;
      false_alerts_per_aoi_year: number | null;
      median_delay_days: number | null;
    }[];
  } | null;
  retraining: { labels: number; queued_utc: string } | null;
}

export interface Limits {
  max_area_km2: number;
}

/** Fired when the API says the session is gone, so the app can ask to sign in again. */
export const SIGNED_OUT = "orbitdiff:signed-out";

export class ApiError extends Error {
  constructor(
    readonly status: number,
    message: string,
  ) {
    super(message);
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let response: Response;
  try {
    response = await fetch(`${API_URL}${path}`, {
      ...init,
      credentials: "include", // the session cookie
      headers: { "content-type": "application/json", ...init?.headers },
    });
  } catch {
    throw new ApiError(0, "The Orbit Diff API is not reachable.");
  }
  if (!response.ok) {
    const body = await response.json().catch(() => null);
    const detail = typeof body?.detail === "string" ? body.detail : response.statusText;
    if (response.status === 401) window.dispatchEvent(new Event(SIGNED_OUT));
    throw new ApiError(response.status, detail);
  }
  const text = await response.text();
  return (text ? JSON.parse(text) : undefined) as T;
}

const json = (method: string, body: unknown): RequestInit => ({
  method,
  body: JSON.stringify(body),
});

export const api = {
  limits: () => request<Limits>("/limits"),
  areas: () => request<AoiSummary[]>("/aois"),
  area: (id: number) => request<AoiDetail>(`/aois/${id}`),
  createArea: (name: string, geometry: Polygon) =>
    request<AoiSummary>("/aois", json("POST", { name, geometry })),
  progress: (id: number) => request<Progress>(`/aois/${id}/progress`),
  backfill: (id: number) => request<Progress>(`/aois/${id}/backfill`, { method: "POST" }),
  timeline: async (id: number) =>
    (await request<Month[]>(`/aois/${id}/timeline`)).map((month) => ({
      ...month,
      tiles: {
        true_color: `${API_URL}${month.tiles.true_color}`,
        ndvi: `${API_URL}${month.tiles.ndvi}`,
        point: `${API_URL}${month.tiles.point}`,
      },
    })),
  ndvi: (id: number) => request<NdviPoint[]>(`/aois/${id}/ndvi`),
  scenes: (id: number, month: string) => request<Scene[]>(`/aois/${id}/scenes?month=${month}`),
  events: (id: number) => request<ChangeEvent[]>(`/aois/${id}/events`),
  notReal: (id: number, eventId: number) =>
    request<void>(`/aois/${id}/events/${eventId}/feedback`, { method: "POST" }),
  alertRule: (id: number) => request<AlertRule>(`/aois/${id}/alert-rule`),
  saveAlertRule: (id: number, settings: AlertSettings) =>
    request<AlertRule>(`/aois/${id}/alert-rule`, json("PUT", settings)),
  previewAlertRule: (id: number, settings: AlertSettings, signal?: AbortSignal) =>
    request<RulePreview>(`/aois/${id}/alert-rule/preview`, {
      ...json("POST", settings),
      signal,
    }),
  me: () => request<Me>("/auth/me"),
  adminSummary: () => request<AdminSummary>("/admin/summary"),
  labels: (id: number) => request<Labels>(`/aois/${id}/labels`),
  saveLabels: (id: number, labels: LabelSet) =>
    request<Labels>(`/aois/${id}/labels`, json("PUT", labels)),
  signIn: (email: string) => request<{ minutes: number }>("/auth/login", json("POST", { email })),
  signOut: () => request<void>("/auth/logout", { method: "POST" }),
};

export const eventImageUrl = (areaId: number, eventId: number, side: "before" | "after") =>
  `${API_URL}/aois/${areaId}/events/${eventId}/${side}.png`;

export const sceneImageUrl = (path: string): string => `${API_URL}${path}`;

/** The composite's NDVI at a point, and whether that pixel was filled for display. */
export async function pointReadout(
  template: string,
  [lon, lat]: LonLat,
  signal: AbortSignal,
): Promise<{ ndvi: number | null; filled: boolean } | null> {
  const url = template.replace("{lon}", lon.toFixed(6)).replace("{lat}", lat.toFixed(6));
  const response = await fetch(url, { signal, credentials: "include" });
  if (!response.ok) return null; // outside the composite
  const { values } = (await response.json()) as { values: (number | null)[] };
  const [ndvi, filled] = values;
  return {
    ndvi: typeof ndvi === "number" && Number.isFinite(ndvi) ? ndvi : null,
    filled: filled === 1,
  };
}
