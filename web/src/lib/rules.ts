/** Alert-rule settings as the form edits them. Whether an event alerts is the API's call. */

import type { AlertSettings } from "../api";

export const settingsOf = ({
  enabled,
  categories,
  skip_seasonal,
  min_ha,
}: AlertSettings): AlertSettings => ({ enabled, categories, skip_seasonal, min_ha });

export function sameSettings(a: AlertSettings, b: AlertSettings): boolean {
  return (
    a.enabled === b.enabled &&
    a.skip_seasonal === b.skip_seasonal &&
    a.min_ha === b.min_ha &&
    a.categories.length === b.categories.length &&
    a.categories.every((c) => b.categories.includes(c))
  );
}

/** Years from the first month to the last, both included: "2017-01-01", "2026-10-01" -> 9.83. */
export function archiveYears(first: string | null, last: string | null): number {
  if (!first || !last) return 0;
  const months = (iso: string) => Number(iso.slice(0, 4)) * 12 + Number(iso.slice(5, 7));
  return (months(last) - months(first) + 1) / 12;
}

/** "about 7.5 a year", or null when the archive is too short to say. */
export function perYear(count: number, years: number): string | null {
  if (years < 1) return null;
  const rate = count / years;
  return `about ${rate < 10 ? rate.toFixed(1) : Math.round(rate)} a year`;
}
