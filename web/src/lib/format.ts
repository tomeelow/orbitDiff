/** Figures as users read them. Times stay in UTC, the time the pipeline works in. */

export function formatBytes(bytes: number): string {
  const units = ["B", "kB", "MB", "GB", "TB"];
  let value = bytes;
  let unit = 0;
  while (value >= 1000 && unit < units.length - 1) {
    value /= 1000;
    unit += 1;
  }
  const digits = value >= 100 || unit === 0 ? 0 : 1;
  return `${value.toFixed(digits)} ${units[unit]}`;
}

/** Hectares under 100 ha, km² above. */
export function formatArea(hectares: number): string {
  return hectares < 100 ? `${hectares.toFixed(1)} ha` : `${(hectares / 100).toFixed(2)} km²`;
}

export const formatPercent = (frac: number): string => `${Math.round(frac * 100)}%`;

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

/** "2026-08-02T09:16:22Z" -> "2 Aug 09:16". */
export function formatUtc(iso: string): string {
  const t = new Date(iso);
  const hh = String(t.getUTCHours()).padStart(2, "0");
  const mm = String(t.getUTCMinutes()).padStart(2, "0");
  return `${t.getUTCDate()} ${MONTHS[t.getUTCMonth()]} ${hh}:${mm}`;
}

export function formatLonLat(lon: number, lat: number): string {
  return `${Math.abs(lat).toFixed(5)}° ${lat >= 0 ? "N" : "S"}, ${Math.abs(lon).toFixed(5)}° ${lon >= 0 ? "E" : "W"}`;
}
