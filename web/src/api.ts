/** The Orbit Diff API, typed as it answers (src/orbitdiff/api.py). */

import type { LonLat, Polygon } from "./lib/geo";

export const API_URL: string = import.meta.env.VITE_API_URL ?? "http://localhost:8000";

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
  tiles: { true_color: string; ndvi: string; point: string };
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
  detected_utc: string;
  status_utc: string;
  history: StatusChange[];
  items: string[];
  method_version: string;
  alerts: boolean; // under the area's alert rule, decided by the API
  alert_reason: string;
}

export interface Limits {
  max_area_km2: number;
}

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
      headers: { "content-type": "application/json", ...init?.headers },
    });
  } catch {
    throw new ApiError(0, "The Orbit Diff API is not reachable.");
  }
  if (!response.ok) {
    const body = await response.json().catch(() => null);
    const detail = typeof body?.detail === "string" ? body.detail : response.statusText;
    throw new ApiError(response.status, detail);
  }
  return response.json() as Promise<T>;
}

export const api = {
  limits: () => request<Limits>("/limits"),
  areas: () => request<AoiSummary[]>("/aois"),
  area: (id: number) => request<AoiDetail>(`/aois/${id}`),
  createArea: (name: string, geometry: Polygon) =>
    request<AoiSummary>("/aois", { method: "POST", body: JSON.stringify({ name, geometry }) }),
  progress: (id: number) => request<Progress>(`/aois/${id}/progress`),
  backfill: (id: number) => request<Progress>(`/aois/${id}/backfill`, { method: "POST" }),
  timeline: (id: number) => request<Month[]>(`/aois/${id}/timeline`),
  ndvi: (id: number) => request<NdviPoint[]>(`/aois/${id}/ndvi`),
  scenes: (id: number, month: string) => request<Scene[]>(`/aois/${id}/scenes?month=${month}`),
  events: (id: number) => request<ChangeEvent[]>(`/aois/${id}/events`),
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
  const response = await fetch(url, { signal });
  if (!response.ok) return null; // outside the composite
  const { values } = (await response.json()) as { values: (number | null)[] };
  const [ndvi, filled] = values;
  return {
    ndvi: typeof ndvi === "number" && Number.isFinite(ndvi) ? ndvi : null,
    filled: filled === 1,
  };
}
