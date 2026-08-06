// Journey planning: RAPTOR over a region-day, built from a pack.
//
// A port of planner.py, kept structurally close to it so the two can be
// read side by side. Pure routing: no realtime, no HTTP, no map. Times are
// seconds since midnight of the service date in the region's zone, and the
// caller converts to and from epochs — see PLANNER.md for the design.
//
// Two places where JavaScript needs care that Python did not:
//
//   * Iteration order. Python sets of small integers iterate in ascending
//     order by accident of hashing; a JS Set iterates in insertion order.
//     RAPTOR's scan order is visible in its tie-breaks, so anywhere the
//     original relies on that accident, this sorts explicitly.
//   * Trip order within a route. Python sorts tuples, which compares the
//     first departure and then the trip id; that comparison is spelled out
//     here so both sides pick the same trip when two leave together.

import { type Db, marks } from "./db.ts";

export const MAX_ROUNDS = 4;             // up to 3 transfers
export const WALK_SPEED_MPS = 80 / 60;   // 80 m/min
export const WALK_DETOUR = 1.3;          // straight line -> street distance
export const WALK_RADIUS_M = 200;        // footpath generation radius
export const MIN_TRANSFER_S = 120;       // even "across the platform"
export const STATION_TRANSFER_S = 180;   // between platforms of one station
const INF = Infinity;

export interface TripRun {
  tripId: string;
  arrs: number[];
  deps: number[];
}

export interface Timetable {
  stopIds: string[];
  stopIndex: Map<string, number>;
  routes: Array<{ stops: number[]; trips: TripRun[] }>;
  stopRoutes: Map<number, Array<[number, number]>>;   // stop -> [route, pos]
  foot: Map<number, Array<[number, number]>>;         // stop -> [stop, secs]
  groupOf: Map<string, Set<string>>;
}

export interface ServiceDate {
  ymd: string;
  /** Seconds to shift this date's times by: today 0, yesterday -86400. */
  offset: number;
}

export type Leg =
  | { kind: "walk"; from: string; to: string; secs: number }
  | { kind: "ride"; tripId: string; board: string; alight: string;
      depSec: number; arrSec: number };

export interface Itinerary {
  legs: Leg[];
  arrSec: number;
  depSec: number;
  rides: number;
}

function haversineM(lat1: number, lon1: number, lat2: number, lon2: number): number {
  const rad = Math.PI / 180;
  const p1 = lat1 * rad, p2 = lat2 * rad;
  const a = Math.sin((p2 - p1) / 2) ** 2
          + Math.cos(p1) * Math.cos(p2) * Math.sin(((lon2 - lon1) * rad) / 2) ** 2;
  return 2 * 6371000 * Math.asin(Math.sqrt(a));
}

const WEEKDAY = ["sunday", "monday", "tuesday", "wednesday", "thursday",
                 "friday", "saturday"] as const;

function activeServiceIds(db: Db, ymd: string, tz: string): string[] {
  // Midday, so no question of which day a midnight instant belongs to.
  const y = Number(ymd.slice(0, 4)), m = Number(ymd.slice(4, 6)),
        d = Number(ymd.slice(6, 8));
  const name = new Intl.DateTimeFormat("en-US", { timeZone: tz, weekday: "short" })
    .format(new Date(Date.UTC(y, m - 1, d, 12)));
  const col = WEEKDAY[["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"]
    .indexOf(name)] ?? "monday";
  const ids = new Set<string>();
  for (const r of db.all(
    `SELECT service_id FROM calendar
      WHERE ${col} = 1 AND start_date <= ? AND end_date >= ?`, [ymd, ymd])) {
    ids.add(String(r["service_id"]));
  }
  for (const r of db.all(
    "SELECT service_id, exception_type FROM calendar_dates WHERE date = ?", [ymd])) {
    const id = String(r["service_id"]);
    if (Number(r["exception_type"]) === 1) ids.add(id);
    else ids.delete(id);
  }
  return [...ids];
}

const outOfService = (t: unknown): boolean =>
  typeof t === "string" && t.toLowerCase().includes("out of service");

/**
 * Build RAPTOR's view of one region-day.
 *
 * `dates` is today at offset 0 plus yesterday at -86400, whose
 * after-midnight tail is the only part that survives into today's clock.
 */
export function buildTimetable(
  db: Db, dates: ServiceDate[], tz: string, tsnFamily = false,
): Timetable {
  const tt: Timetable = {
    stopIds: [], stopIndex: new Map(), routes: [],
    stopRoutes: new Map(), foot: new Map(), groupOf: new Map(),
  };

  // --- stops: all of them, because footpaths and groups want positions ---
  const stopPos = new Map<string, [number, number]>();
  const parentChildren = new Map<string, string[]>();
  for (const r of db.all(
    "SELECT stop_id, stop_lat, stop_lon, parent_station FROM stops")) {
    const sid = String(r["stop_id"]);
    tt.stopIndex.set(sid, tt.stopIds.length);
    tt.stopIds.push(sid);
    if (r["stop_lat"] !== null && r["stop_lon"] !== null) {
      stopPos.set(sid, [Number(r["stop_lat"]), Number(r["stop_lon"])]);
    }
    const parent = r["parent_station"] ? String(r["parent_station"]) : "";
    if (parent) {
      const list = parentChildren.get(parent) ?? [];
      list.push(sid);
      parentChildren.set(parent, list);
    }
  }

  // --- the out-of-service filter ----------------------------------------
  const oosRoutes = new Set<string>();
  for (const r of db.all("SELECT route_id, route_short_name, route_long_name FROM routes")) {
    if (outOfService(r["route_short_name"]) || outOfService(r["route_long_name"])) {
      oosRoutes.add(String(r["route_id"]));
    }
  }

  // --- per-trip calls, for both service dates ---------------------------
  // Keyed (trip, offset): the same trip can run on both days, and the two
  // runs are different journeys 24 hours apart.
  const byTrip = new Map<string, { tripId: string; stops: number[];
                                   arrs: number[]; deps: number[] }>();
  for (const { ymd, offset } of dates) {
    const sids = activeServiceIds(db, ymd, tz);
    if (!sids.length) continue;
    for (const r of db.all(
      `SELECT st.trip, st.stop, st.arr, st.dep, t.trip_id, t.trip_headsign,
              t.route_id
         FROM stop_times st JOIN trips t ON t.trip = st.trip
        WHERE t.service_id IN (${marks(sids.length)})
        ORDER BY t.trip_id, st.seq`, sids)) {
      if (oosRoutes.has(String(r["route_id"])) || outOfService(r["trip_headsign"])) {
        continue;
      }
      const arr = r["arr"] ?? r["dep"], dep = r["dep"] ?? r["arr"];
      if (arr === null || dep === null) continue;
      const key = `${r["trip"]}|${offset}`;
      let cur = byTrip.get(key);
      if (!cur) {
        cur = { tripId: String(r["trip_id"]), stops: [], arrs: [], deps: [] };
        byTrip.set(key, cur);
      }
      cur.stops.push(Number(r["stop"]));
      cur.arrs.push(Number(arr) + offset);
      cur.deps.push(Number(dep) + offset);
    }
  }

  // The pack's stop ids are dense integers already; map them to RAPTOR's
  // indices once rather than per call.
  const packStopToIndex = new Map<number, number>();
  for (const r of db.all("SELECT stop, stop_id FROM stops")) {
    const i = tt.stopIndex.get(String(r["stop_id"]));
    if (i !== undefined) packStopToIndex.set(Number(r["stop"]), i);
  }

  // --- group trips into routes: identical stop sequences ----------------
  const grouped = new Map<string, Array<{ dep0: number; run: TripRun; stops: number[] }>>();
  for (const cur of byTrip.values()) {
    if (cur.stops.length < 2) continue;
    const offsetNegative = cur.arrs[cur.arrs.length - 1]! < 0;
    if (offsetNegative) continue;    // yesterday's trip finished before midnight
    const idxs: number[] = [];
    let ok = true;
    for (const s of cur.stops) {
      const i = packStopToIndex.get(s);
      if (i === undefined) { ok = false; break; }
      idxs.push(i);
    }
    if (!ok) continue;
    const key = idxs.join(",");
    const list = grouped.get(key) ?? [];
    list.push({ dep0: cur.deps[0]!, stops: idxs,
                run: { tripId: cur.tripId, arrs: cur.arrs, deps: cur.deps } });
    grouped.set(key, list);
  }
  for (const list of grouped.values()) {
    // Python sorts the tuple (first departure, trip id, ...); same order here.
    list.sort((a, b) => a.dep0 - b.dep0
      || (a.run.tripId < b.run.tripId ? -1 : a.run.tripId > b.run.tripId ? 1 : 0));
    const rIdx = tt.routes.length;
    const stops = list[0]!.stops;
    tt.routes.push({ stops, trips: list.map((t) => t.run) });
    const seen = new Set<number>();
    stops.forEach((s, pos) => {
      if (seen.has(s)) return;        // loops: board at the first visit
      seen.add(s);
      const at = tt.stopRoutes.get(s) ?? [];
      at.push([rIdx, pos]);
      tt.stopRoutes.set(s, at);
    });
  }

  // --- footpaths: grid-bucket, then exact haversine in a 3x3 -------------
  const CELL = 0.002;                 // ~220 m of latitude
  const grid = new Map<string, string[]>();
  for (const [sid, [lat, lon]] of stopPos) {
    const k = `${Math.floor(lat / CELL)},${Math.floor(lon / CELL)}`;
    const list = grid.get(k) ?? [];
    list.push(sid);
    grid.set(k, list);
  }
  const addFoot = (i: number, j: number, secs: number) => {
    const list = tt.foot.get(i) ?? [];
    list.push([j, secs]);
    tt.foot.set(i, list);
  };
  for (const [k, members] of grid) {
    const [gy, gx] = k.split(",").map(Number) as [number, number];
    const neighbours: string[] = [];
    for (let dy = -1; dy <= 1; dy++) {
      for (let dx = -1; dx <= 1; dx++) {
        neighbours.push(...(grid.get(`${gy + dy},${gx + dx}`) ?? []));
      }
    }
    for (const sid of members) {
      const [la, lo] = stopPos.get(sid)!;
      const i = tt.stopIndex.get(sid)!;
      for (const oid of neighbours) {
        if (oid <= sid) continue;     // each unordered pair once
        const [lb, lg] = stopPos.get(oid)!;
        const dist = haversineM(la, lo, lb, lg);
        if (dist > WALK_RADIUS_M) continue;
        const secs = Math.max(MIN_TRANSFER_S,
                              Math.trunc((dist * WALK_DETOUR) / WALK_SPEED_MPS));
        const j = tt.stopIndex.get(oid)!;
        addFoot(i, j, secs);
        addFoot(j, i, secs);
      }
    }
  }

  // --- station groups: parent + children, and TfNSW TSN siblings ---------
  const groups = new Map<string, Set<string>>();
  for (const [parent, children] of parentChildren) {
    const g = new Set(children);
    if (tt.stopIndex.has(parent)) g.add(parent);
    groups.set(`p:${parent}`, g);
  }
  if (tsnFamily) {
    const byTsn = new Map<string, Set<string>>();
    for (const sid of tt.stopIds) {
      const i = sid.indexOf(":");
      if (i === -1) continue;
      const tsn = sid.slice(i + 1);
      const set = byTsn.get(tsn) ?? new Set();
      set.add(sid);
      byTsn.set(tsn, set);
    }
    for (const [tsn, members] of byTsn) {
      if (members.size > 1) groups.set(`t:${tsn}`, members);
    }
  }
  for (const members of groups.values()) {
    const idxs = [...members].map((s) => tt.stopIndex.get(s))
      .filter((i): i is number => i !== undefined);
    for (const i of idxs) {
      for (const j of idxs) if (i !== j) addFoot(i, j, STATION_TRANSFER_S);
    }
    for (const s of members) {
      const g = tt.groupOf.get(s) ?? new Set<string>();
      for (const m of members) g.add(m);
      tt.groupOf.set(s, g);
    }
  }
  return tt;
}

function expand(tt: Timetable, stopId: string): number[] {
  const ids = tt.groupOf.get(stopId) ?? new Set([stopId]);
  return [...ids].map((s) => tt.stopIndex.get(s))
    .filter((i): i is number => i !== undefined);
}

export interface PlanOptions {
  fromStop?: string;
  toStop?: string;
  /** An ADDRESS origin: each walkable stop pre-charged its walking time. */
  sources?: Array<[string, number]>;
  /** An address destination: each stop carries the walk from it to the door. */
  targets?: Array<[string, number]>;
  maxItineraries?: number;
}

/**
 * RAPTOR: earliest arrival per transfer count, then a pareto trim.
 *
 * Arrival means arrival AT THE DOOR — the walk from each candidate
 * alighting stop weighs into which one wins, which is why a stop further
 * from the station can be the right one to get off at.
 */
export function plan(tt: Timetable, depSec: number, opts: PlanOptions): Itinerary[] {
  const srcPens = new Map<number, number>();
  if (opts.sources) {
    for (const [sid, pen] of opts.sources) {
      for (const i of expand(tt, sid)) {
        if (pen < (srcPens.get(i) ?? INF)) srcPens.set(i, pen);
      }
    }
  } else if (opts.fromStop) {
    for (const i of expand(tt, opts.fromStop)) srcPens.set(i, 0);
  }
  const dstPen = new Map<number, number>();
  if (opts.targets) {
    for (const [sid, pen] of opts.targets) {
      for (const i of expand(tt, sid)) {
        if (pen < (dstPen.get(i) ?? INF)) dstPen.set(i, pen);
      }
    }
  } else if (opts.toStop) {
    for (const i of expand(tt, opts.toStop)) dstPen.set(i, 0);
  }
  if (!srcPens.size || !dstPen.size) return [];

  const tau: Array<Map<number, number>> = [];
  const parent: Array<Map<number, unknown[]>> = [];
  for (let k = 0; k <= MAX_ROUNDS; k++) { tau.push(new Map()); parent.push(new Map()); }
  const best = new Map<number, number>();

  const settle = (k: number, stop: number, t: number, via: unknown[]): boolean => {
    if (t < (best.get(stop) ?? INF) && t < (tau[k]!.get(stop) ?? INF)) {
      tau[k]!.set(stop, t);
      best.set(stop, t);
      parent[k]!.set(stop, via);
      return true;
    }
    return false;
  };

  let marked = new Set<number>();
  for (const [s, pen] of srcPens) {
    const t0 = depSec + pen;
    if (t0 < (tau[0]!.get(s) ?? INF)) {
      tau[0]!.set(s, t0);
      best.set(s, t0);
      marked.add(s);
    }
  }
  // Walking straight from the origin counts as round 0.
  for (const s of [...marked].sort((a, b) => a - b)) {
    for (const [v, w] of tt.foot.get(s) ?? []) {
      if (settle(0, v, tau[0]!.get(s)! + w, ["walk", s, 0, w])) marked.add(v);
    }
  }

  for (let k = 1; k <= MAX_ROUNDS; k++) {
    const queue = new Map<number, number>();
    for (const s of [...marked].sort((a, b) => a - b)) {
      for (const [rIdx, pos] of tt.stopRoutes.get(s) ?? []) {
        const have = queue.get(rIdx);
        if (have === undefined || pos < have) queue.set(rIdx, pos);
      }
    }
    marked = new Set();

    for (const [rIdx, startPos] of queue) {
      const { stops, trips } = tt.routes[rIdx]!;
      let trip: TripRun | null = null;
      let boardPos = -1;
      for (let pos = startPos; pos < stops.length; pos++) {
        const s = stops[pos]!;
        if (trip !== null) {
          const arr = trip.arrs[pos]!;
          if (arr < (best.get(s) ?? INF)) {
            if (settle(k, s, arr, ["ride", trip.tripId, stops[boardPos]!,
                                   trip.deps[boardPos]!, pos, boardPos, rIdx])) {
              marked.add(s);
            }
          }
        }
        // Can an earlier (or the first) trip be caught here?
        const tPrev = tau[k - 1]!.get(s);
        if (tPrev !== undefined && pos < stops.length - 1) {
          if (trip === null || tPrev < trip.deps[pos]!) {
            let lo = 0, hi = trips.length;
            while (lo < hi) {
              const mid = (lo + hi) >> 1;
              if (trips[mid]!.deps[pos]! < tPrev) lo = mid + 1;
              else hi = mid;
            }
            if (lo < trips.length
                && (trip === null || trips[lo]!.deps[pos]! < trip.deps[pos]!)) {
              trip = trips[lo]!;
              boardPos = pos;
            } else if (lo < trips.length && trip !== null
                       && trips[lo]!.tripId === trip.tripId) {
              // The SAME trip is catchable here too. Board wherever leaves
              // the most slack between arriving at the stop and the bus
              // going — boarding at the first stop scanned rode people
              // straight past their nearest one.
              const tB = tau[k - 1]!.get(stops[boardPos]!);
              if (tB !== undefined
                  && trip.deps[pos]! - tPrev > trip.deps[boardPos]! - tB) {
                boardPos = pos;
              }
            }
          }
        }
      }
    }

    // Footpath relaxation within the round.
    for (const s of [...marked].sort((a, b) => a - b)) {
      const t = tau[k]!.get(s)!;
      for (const [v, w] of tt.foot.get(s) ?? []) {
        if (settle(k, v, t + w, ["walk", s, k, w])) marked.add(v);
      }
    }
    if (!marked.size) break;
  }

  /** Walk the parent chain back to a seed: (legs, when to leave the door). */
  const rebuild = (kk: number, stop: number): [Leg[] | null, number] => {
    const legs: Leg[] = [];
    let k = kk, s = stop, guard = 0;
    for (;;) {
      if (++guard > 50) return [null, 0];
      const via = parent[k]!.get(s);
      if (via === undefined) break;               // reached a source stop
      if (via[0] === "walk") {
        const [, prev, kw, w] = via as [string, number, number, number];
        legs.push({ kind: "walk", from: tt.stopIds[prev]!, to: tt.stopIds[s]!, secs: w });
        s = prev; k = kw;
      } else {
        const [, tid, boardIdx, depS, pos, , rIdx] =
          via as [string, string, number, number, number, number, number];
        let arrS = 0;
        for (const t of tt.routes[rIdx]!.trips) {
          if (t.tripId === tid) { arrS = t.arrs[pos]!; break; }
        }
        legs.push({ kind: "ride", tripId: tid, board: tt.stopIds[boardIdx]!,
                    alight: tt.stopIds[s]!, depSec: depS, arrSec: arrS });
        s = boardIdx; k -= 1;
      }
    }
    if (!legs.length) return [null, 0];
    legs.reverse();
    // When you must leave the door: the first ride's departure, minus every
    // walk before it — footpath legs and the seed stop's own walk penalty.
    let setOff = depSec;
    for (const l of legs) if (l.kind === "ride") { setOff = l.depSec; break; }
    let lead = srcPens.get(s) ?? 0;
    for (const l of legs) {
      if (l.kind === "ride") break;
      lead += l.secs;
    }
    return [legs, setOff - lead];
  };

  const out: Itinerary[] = [];
  let seenArr = INF;
  for (let k = 1; k <= MAX_ROUNDS; k++) {
    let arr = INF;
    for (const [d, pen] of dstPen) {
      const t = tau[k]!.get(d);
      if (t !== undefined && t + pen < arr) arr = t + pen;
    }
    if (arr >= seenArr || arr === INF) continue;
    seenArr = arr;
    // Several alighting stops can reach the door at the same moment: keep
    // the journey you can leave for LATEST, then the shortest final walk.
    // Sorted, so a genuine three-way tie resolves the same way every run.
    // (The server resolves it by Python set iteration order, which is a
    // hash-table artefact — when two platforms of one station tie exactly,
    // the two implementations can name different platforms of the same
    // station. Same journey, same minute, different sign on the wall.)
    let bestRank: [number, number] | null = null;
    let bestLegs: Leg[] | null = null;
    for (const [d, pen] of [...dstPen].sort((a, b) => a[0] - b[0])) {
      const t = tau[k]!.get(d);
      if (t === undefined || t + pen !== arr) continue;
      const [legs, setOff] = rebuild(k, d);
      if (!legs) continue;
      const rank: [number, number] = [setOff, -pen];
      if (bestRank === null || rank[0] > bestRank[0]
          || (rank[0] === bestRank[0] && rank[1] > bestRank[1])) {
        bestRank = rank;
        bestLegs = legs;
      }
    }
    if (bestLegs) {
      const firstRide = bestLegs.find((l) => l.kind === "ride");
      out.push({
        legs: bestLegs,
        arrSec: arr,
        depSec: firstRide && firstRide.kind === "ride" ? firstRide.depSec : depSec,
        rides: bestLegs.filter((l) => l.kind === "ride").length,
      });
    }
    if (out.length >= (opts.maxItineraries ?? 4)) break;
  }
  return out;
}
