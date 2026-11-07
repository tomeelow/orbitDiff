/** Geometry for drawing an area: its size on the ground and its bounds.

The API measures the final area in the AOI's UTM zone; this is the spherical
approximation shown while drawing, close enough to show the cap before submitting. */

export type LonLat = [number, number];
export type Ring = LonLat[];
export interface Polygon {
  type: "Polygon";
  coordinates: Ring[];
}
export type Bounds = [west: number, south: number, east: number, north: number];

const EARTH_RADIUS_M = 6378137; // WGS 84 semi-major axis, as in Turf
const rad = (deg: number) => (deg * Math.PI) / 180;

/** Area of a lon/lat ring in square metres (Chamberlain and Duquette, 2007). */
export function ringAreaM2(ring: Ring): number {
  if (ring.length < 3) return 0;
  let total = 0;
  ring.forEach(([lon1, lat1], i) => {
    const [lon2, lat2] = ring[(i + 1) % ring.length] as LonLat;
    total += rad(lon2 - lon1) * (2 + Math.sin(rad(lat1)) + Math.sin(rad(lat2)));
  });
  return Math.abs((total * EARTH_RADIUS_M * EARTH_RADIUS_M) / 2);
}

/** Outer ring minus holes, in km². */
export function polygonAreaKm2(polygon: Polygon): number {
  const [outer, ...holes] = polygon.coordinates;
  if (!outer) return 0;
  const m2 = holes.reduce((area, hole) => area - ringAreaM2(hole), ringAreaM2(outer));
  return m2 / 1e6;
}

export function boundsOf(points: LonLat[]): Bounds {
  const lons = points.map((p) => p[0]);
  const lats = points.map((p) => p[1]);
  return [Math.min(...lons), Math.min(...lats), Math.max(...lons), Math.max(...lats)];
}
