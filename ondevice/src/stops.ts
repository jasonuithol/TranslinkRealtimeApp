// Finding a stop: by name, or by being near one.
//
// Ported from boards.py. Both answers hide child platforms and return the
// parent station once — a rider looking for "Roma Street" wants the
// station, not its eleven faces.

import { type Db, num } from "./db.ts";

export interface NearStop {
  stopId: string;
  name: string;
  lat: number;
  lon: number;
  locationType: number | null;
  distM: number;
}

export function haversineM(
  lat1: number, lon1: number, lat2: number, lon2: number,
): number {
  const rad = Math.PI / 180;
  const p1 = lat1 * rad, p2 = lat2 * rad;
  const a = Math.sin((p2 - p1) / 2) ** 2
          + Math.cos(p1) * Math.cos(p2) * Math.sin(((lon2 - lon1) * rad) / 2) ** 2;
  return 2 * 6371000 * Math.asin(Math.sqrt(a));
}

/**
 * The closest stops to a point.
 *
 * A ~2.2 km box first, haversine only on what survives it: the box is an
 * index range, the trigonometry is not.
 */
export function nearbyStops(
  db: Db, lat: number, lon: number, limit = 10,
): NearStop[] {
  const dlat = 0.02;
  const dlon = 0.02 / Math.max(0.2, Math.cos((lat * Math.PI) / 180));
  const rows = db.all(
    `SELECT stop_id, stop_name, stop_lat, stop_lon, location_type
       FROM stops
      WHERE (parent_station IS NULL OR parent_station = '')
        AND stop_lat BETWEEN ? AND ? AND stop_lon BETWEEN ? AND ?`,
    [lat - dlat, lat + dlat, lon - dlon, lon + dlon]);

  const out: NearStop[] = [];
  for (const r of rows) {
    const la = num(r["stop_lat"]), lo = num(r["stop_lon"]);
    if (la === null || lo === null) continue;
    out.push({
      stopId: String(r["stop_id"]),
      name: String(r["stop_name"] ?? ""),
      lat: la, lon: lo,
      locationType: num(r["location_type"]),
      distM: Math.round(haversineM(lat, lon, la, lo)),
    });
  }
  out.sort((a, b) => a.distM - b.distM);
  return out.slice(0, limit);
}

/** Stops whose name contains every word typed, nearest-first if a point is given. */
export function searchStops(
  db: Db, query: string, limit = 10, near?: { lat: number; lon: number },
): NearStop[] {
  const words = query.trim().toLowerCase().split(/\s+/).filter(Boolean);
  if (!words.length) return [];
  const where = words.map(() => "lower(stop_name) LIKE ?").join(" AND ");
  const rows = db.all(
    `SELECT stop_id, stop_name, stop_lat, stop_lon, location_type
       FROM stops
      WHERE (parent_station IS NULL OR parent_station = '') AND ${where}
      LIMIT 200`,
    words.map((w) => `%${w}%`));

  const out: NearStop[] = rows.map((r) => {
    const la = num(r["stop_lat"]) ?? 0, lo = num(r["stop_lon"]) ?? 0;
    return {
      stopId: String(r["stop_id"]),
      name: String(r["stop_name"] ?? ""),
      lat: la, lon: lo,
      locationType: num(r["location_type"]),
      distM: near ? Math.round(haversineM(near.lat, near.lon, la, lo)) : 0,
    };
  });
  // Proximity decides between same-named stops in different suburbs; without
  // a reference point, the shorter name is the more likely intent.
  if (near) out.sort((a, b) => a.distM - b.distM);
  else out.sort((a, b) => a.name.length - b.name.length);
  return out.slice(0, limit);
}
