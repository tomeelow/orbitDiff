/** How change events read: category names and colours, statuses, alert verdicts and the
status history. The API decides whether an event alerts; this only words its answer. */

import type { Category, ChangeEvent, EventStatus, StatusChange } from "../api";
import { formatUtcDate } from "./format";
import { monthLabel } from "./months";

/** Map outlines sit on satellite imagery in both themes, so one set of hex colours. */
export const CATEGORY: Record<Category, { title: string; color: string }> = {
  buildings_roads: { title: "Buildings and roads", color: "#e254a6" },
  vegetation_loss: { title: "Vegetation loss", color: "#ffd23f" },
  solar_industry: { title: "Solar and industry", color: "#9e7bff" },
  water: { title: "Water", color: "#3cc6f0" },
  other: { title: "Other change", color: "#c9c2b6" },
};

export const STATUS: Record<EventStatus, string> = {
  pending: "New · unconfirmed",
  confirmed: "Confirmed",
  dismissed: "Dismissed",
  seasonal: "Seasonal",
};

/** The API's alert verdict ("alert", "seasonal", "under 0.5 ha", ...) as a tag. */
export function alertText(event: Pick<ChangeEvent, "alerts" | "alert_reason">): string {
  if (event.alerts) return "Would alert";
  const reason = event.alert_reason;
  if (reason === "category off") return "Category not watched";
  if (reason === "alerts off") return "Alerts off";
  if (reason === "dismissed" || reason === "seasonal") return "No alert";
  return reason.charAt(0).toUpperCase() + reason.slice(1); // "Under 0.5 ha"
}

/** "Seen 1 Apr 2020 on S2A_…", "Confirmed 8 Apr 2020 by S2A_…", "Seasonal: snow in Jan 2021". */
export function historyLine(change: StatusChange, first: boolean): string {
  if (!/^S2[A-D]_/.test(change.cause)) {
    const cause = change.cause.replace(/\b\d{4}-\d{2}\b/g, (key) => monthLabel(key));
    return `${STATUS[change.status]}: ${cause}`;
  }
  const verb = first && change.status === "pending" ? "Seen" : STATUS[change.status];
  return `${verb} ${formatUtcDate(change.at)} ${first ? "on" : "by"} ${change.cause}`;
}
