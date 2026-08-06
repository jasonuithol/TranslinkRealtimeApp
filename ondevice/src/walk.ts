// Walking: routes along real streets, and the names of those streets.
//
// A port of walking.py. Two differences from the server, both from the
// pack: coordinates are micro-degree integers, and the nearest-node lookup
// scans an id range per cell instead of probing a spatial index — the pack
// numbers its nodes in cell order precisely so that works (build_pack.py).

import { type Db, coordScale, marks } from "./db.ts";

const CELL_DEG = 0.01;
const M_PER_DEG_LAT = 111000.0;
const M_PER_DEG_LON = 88000.0;

export interface WalkRoute {
  points: Array<[number, number]>;      // [lon, lat], door to door
  distM: number;
  streets: StreetRun[];
}

export interface StreetRun {
  name: string;
  m: number;
  line: Array<[number, number]>;
}

const cellKey = (lat: number, lon: number): number =>
  Math.floor((lat + 90) / CELL_DEG) * 40000 + Math.floor((lon + 180) / CELL_DEG);

export function haversineM(la1: number, lo1: number, la2: number, lo2: number): number {
  const rad = Math.PI / 180;
  const p1 = la1 * rad, p2 = la2 * rad;
  const a = Math.sin((p2 - p1) / 2) ** 2
          + Math.cos(p1) * Math.cos(p2) * Math.sin(((lo2 - lo1) * rad) / 2) ** 2;
  return 2 * 6371000 * Math.asin(Math.sqrt(a));
}

interface Node { id: number; lat: number; lon: number }

/** Nearest graph node, searching outward ring by ring (3 rings ~ 3 km). */
export function nearestNode(db: Db, lat: number, lon: number): Node | null {
  const base = cellKey(lat, lon);
  for (let ring = 0; ring < 3; ring++) {
    const cells: number[] = [];
    for (let dy = -ring; dy <= ring; dy++) {
      for (let dx = -ring; dx <= ring; dx++) cells.push(base + dy * 40000 + dx);
    }
    const ranges = db.all(
      `SELECT lo, hi FROM cells WHERE cell IN (${marks(cells.length)})`, cells);
    if (!ranges.length) continue;
    const where = ranges.map(() => "(id BETWEEN ? AND ?)").join(" OR ");
    const rows = db.all(
      `SELECT id, lat, lon FROM nodes WHERE ${where}`,
      ranges.flatMap((r) => [Number(r["lo"]), Number(r["hi"])]));
    if (!rows.length) continue;
    const scale = coordScale(db);
    const coslat = Math.cos((lat * Math.PI) / 180) ** 2;
    let best: Node | null = null, bd = Infinity;
    for (const r of rows) {
      const nlat = Number(r["lat"]) / scale, nlon = Number(r["lon"]) / scale;
      const d = (nlon - lon) ** 2 * coslat + (nlat - lat) ** 2;
      if (d < bd) { bd = d; best = { id: Number(r["id"]), lat: nlat, lon: nlon }; }
    }
    if (best) return best;
  }
  return null;
}

/**
 * A binary heap. The server gets one from Python's standard library; a
 * walk across a suburb pops tens of thousands of times and an array
 * re-sorted on every push turns a 40 ms route into several seconds.
 */
class MinHeap {
  private keys: number[] = [];
  private vals: number[] = [];
  get size(): number { return this.keys.length; }
  push(key: number, val: number): void {
    this.keys.push(key); this.vals.push(val);
    let i = this.keys.length - 1;
    while (i > 0) {
      const p = (i - 1) >> 1;
      if (this.keys[p]! <= this.keys[i]!) break;
      this.swap(i, p); i = p;
    }
  }
  pop(): number | undefined {
    if (!this.keys.length) return undefined;
    const top = this.vals[0]!;
    const k = this.keys.pop()!, v = this.vals.pop()!;
    if (this.keys.length) {
      this.keys[0] = k; this.vals[0] = v;
      let i = 0;
      for (;;) {
        const l = 2 * i + 1, r = l + 1;
        let m = i;
        if (l < this.keys.length && this.keys[l]! < this.keys[m]!) m = l;
        if (r < this.keys.length && this.keys[r]! < this.keys[m]!) m = r;
        if (m === i) break;
        this.swap(i, m); i = m;
      }
    }
    return top;
  }
  private swap(a: number, b: number): void {
    [this.keys[a], this.keys[b]] = [this.keys[b]!, this.keys[a]!];
    [this.vals[a], this.vals[b]] = [this.vals[b]!, this.vals[a]!];
  }
}

export interface WalkOptions {
  /** Refuse anything further than this in a straight line. */
  maxStraightM?: number;
  /** Give up after this many node expansions. */
  maxExpansions?: number;
  /** Name the streets walked (needs the addresses db). */
  addresses?: Db | null;
}

/**
 * Door-to-door walking route, A* over the street graph.
 *
 * Returns null where the server returns 404: too far to be a walk, an area
 * the graph does not cover, or no path between the two. The caller draws a
 * straight line instead, exactly as it does today.
 */
export function walkRoute(
  db: Db, fromLat: number, fromLon: number, toLat: number, toLon: number,
  opts: WalkOptions = {},
): WalkRoute | null {
  const straight = haversineM(fromLat, fromLon, toLat, toLon);
  if (straight > (opts.maxStraightM ?? 2500)) return null;

  const src = nearestNode(db, fromLat, fromLon);
  const dst = nearestNode(db, toLat, toLon);
  if (!src || !dst) return null;

  const nodeScale = coordScale(db);
  const g = new Map<number, number>([[src.id, 0]]);
  const came = new Map<number, number>();
  const pos = new Map<number, [number, number]>([
    [src.id, [src.lat, src.lon]], [dst.id, [dst.lat, dst.lon]]]);
  const open = new MinHeap();
  open.push(0, src.id);

  const limit = opts.maxExpansions ?? 30000;
  let expansions = 0, found = false;
  while (open.size && expansions < limit) {
    const cur = open.pop()!;
    if (cur === dst.id) { found = true; break; }
    expansions++;
    const gcur = g.get(cur)!;
    // The pack clusters edges on (a, b), so a node's neighbours are one
    // contiguous read and there is no separate index to consult.
    for (const e of db.all("SELECT b, d FROM edges WHERE a = ?", [cur])) {
      const b = Number(e["b"]);
      const ng = gcur + Number(e["d"]);
      if (ng >= (g.get(b) ?? Infinity)) continue;
      g.set(b, ng);
      came.set(b, cur);
      let p = pos.get(b);
      if (!p) {
        const r = db.get("SELECT lat, lon FROM nodes WHERE id = ?", [b]);
        if (!r) continue;
        p = [Number(r["lat"]) / nodeScale, Number(r["lon"]) / nodeScale];
        pos.set(b, p);
      }
      open.push(ng + haversineM(p[0], p[1], dst.lat, dst.lon), b);
    }
  }
  if (!found) return null;

  const path: number[] = [dst.id];
  for (;;) {
    const prev = came.get(path[path.length - 1]!);
    if (prev === undefined) break;
    path.push(prev);
  }
  path.reverse();

  const points: Array<[number, number]> = [
    [fromLon, fromLat],
    ...path.map((n) => {
      const p = pos.get(n)!;
      return [p[1], p[0]] as [number, number];
    }),
    [toLon, toLat],
  ];
  return {
    points,
    distM: g.get(dst.id)!,
    streets: opts.addresses ? walkStreets(opts.addresses, points) : [],
  };
}

/**
 * Name the streets a path traverses.
 *
 * The walking graph stores no names, so the nearest address to each ~30 m
 * sample names the street underfoot. Consecutive samples merge into runs
 * and sub-60 m blips — clipping a corner at an intersection — are dropped.
 * Decoration, never structure: empty is a fine answer in a park.
 */
export function walkStreets(
  addresses: Db, points: Array<[number, number]>, step = 30.0,
): StreetRun[] {
  if (points.length < 2) return [];
  const segLen = (a: [number, number], b: [number, number]) =>
    Math.hypot((b[1] - a[1]) * M_PER_DEG_LAT, (b[0] - a[0]) * M_PER_DEG_LON);

  const samples: Array<[number, number]> = [points[0]!];
  let carry = 0;
  for (let i = 0; i + 1 < points.length; i++) {
    const a = points[i]!, b = points[i + 1]!;
    const seg = segLen(a, b);
    if (seg <= 0) continue;
    let t = step - carry;
    while (t <= seg) {
      const f = t / seg;
      samples.push([a[0] + (b[0] - a[0]) * f, a[1] + (b[1] - a[1]) * f]);
      t += step;
    }
    carry = (carry + seg) % step;
  }

  const aScale = coordScale(addresses);
  const names: Array<string | null> = [];
  for (const [lon, lat] of samples) {
    const ranges = cellRangesFor(addresses, lat, lon);
    if (!ranges.length) { names.push(null); continue; }
    const where = ranges.map(() => "(a.id BETWEEN ? AND ?)").join(" OR ");
    const rows = addresses.all(
      `SELECT s.name, s.type, a.lat, a.lon FROM addresses a
         JOIN streets s ON s.id = a.street_id
        WHERE (${where})
          AND a.lat BETWEEN ? AND ? AND a.lon BETWEEN ? AND ?`,
      [...ranges.flat(),
       Math.round((lat - 0.0006) * aScale), Math.round((lat + 0.0006) * aScale),
       Math.round((lon - 0.00075) * aScale), Math.round((lon + 0.00075) * aScale)]);
    let best: string | null = null, bd = 60.0 ** 2;
    for (const r of rows) {
      const ala = Number(r["lat"]) / aScale, alo = Number(r["lon"]) / aScale;
      const d2 = ((ala - lat) * M_PER_DEG_LAT) ** 2
               + ((alo - lon) * M_PER_DEG_LON) ** 2;
      if (d2 < bd) {
        bd = d2;
        const nm = title(String(r["name"]));
        best = r["type"] ? `${nm} ${title(String(r["type"]))}` : nm;
      }
    }
    names.push(best);
  }

  // Collapse into runs, keeping each run's coordinates so the map can draw
  // the name along them; a run of one sample is a corner, not a street.
  const runs: Array<{ name: string | null; n: number; pts: Array<[number, number]> }> = [];
  names.forEach((n, i) => {
    const last = runs[runs.length - 1];
    if (last && last.name === n) { last.n++; last.pts.push(samples[i]!); }
    else runs.push({ name: n, n: 1, pts: [samples[i]!] });
  });
  const out: StreetRun[] = [];
  for (const r of runs) {
    if (r.name === null || r.n < 2) continue;
    const last = out[out.length - 1];
    if (last && last.name === r.name) {
      last.m += Math.trunc(r.n * step);
      last.line.push(...r.pts);
    } else {
      out.push({ name: r.name, m: Math.trunc(r.n * step), line: [...r.pts] });
    }
  }
  return out;
}

function cellRangesFor(db: Db, lat: number, lon: number): Array<[number, number]> {
  const base = cellKey(lat, lon);
  const cells = [base - 40001, base - 40000, base - 39999, base - 1, base,
                 base + 1, base + 39999, base + 40000, base + 40001];
  return db.all(`SELECT lo, hi FROM cells WHERE cell IN (${marks(cells.length)})`, cells)
    .map((r) => [Number(r["lo"]), Number(r["hi"])] as [number, number]);
}

const title = (s: string): string =>
  s.toLowerCase().replace(/(^|[\s'-])([a-z0-9])/g, (_m, p, c) => p + c.toUpperCase());
