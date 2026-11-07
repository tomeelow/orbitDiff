/** Web Mercator tile maths, for preloading the next timelapse frame over the area. */

import type { Bounds } from "./geo";

const lonToX = (lon: number, z: number) => Math.floor(((lon + 180) / 360) * 2 ** z);
function latToY(lat: number, z: number): number {
  const r = (lat * Math.PI) / 180;
  return Math.floor(((1 - Math.log(Math.tan(r) + 1 / Math.cos(r)) / Math.PI) / 2) * 2 ** z);
}

/** Every {z, x, y} tile that touches the bounds at zoom z. */
export function tilesCovering([west, south, east, north]: Bounds, z: number) {
  const tiles: { z: number; x: number; y: number }[] = [];
  for (let x = lonToX(west, z); x <= lonToX(east, z); x++) {
    for (let y = latToY(north, z); y <= latToY(south, z); y++) tiles.push({ z, x, y });
  }
  return tiles;
}

export function tileUrl(template: string, t: { z: number; x: number; y: number }): string {
  return template
    .replace("{z}", String(t.z))
    .replace("{x}", String(t.x))
    .replace("{y}", String(t.y));
}

/** Overlap of two bounds, or null when they don't meet. */
export function intersect(a: Bounds, b: Bounds): Bounds | null {
  const out: Bounds = [
    Math.max(a[0], b[0]),
    Math.max(a[1], b[1]),
    Math.min(a[2], b[2]),
    Math.min(a[3], b[3]),
  ];
  return out[0] < out[2] && out[1] < out[3] ? out : null;
}
