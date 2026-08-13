// Reading the static timetable out of a pack.
//
// A port of gtfsdb.py's half of the board, against the pack schema rather
// than the feed's: trips, stops and shapes carry dense integer ids, the
// feed's own ids sit beside them for realtime matching, and times are
// seconds into the service day instead of "HH:MM:SS". The logic is the
// same logic; only the columns moved. See build_pack.py for why.

import { type Db, type Row, marks, num, str } from "./db.ts";
import { serviceEpoch, weekdayColumn } from "./time.ts";

export interface Stop {
  stop: number;
  stopId: string;
  name: string;
  lat: number | null;
  lon: number | null;
  locationType: number | null;
  parentStation: string | null;
  platformCode: string | null;
}

export interface Departure {
  tripId: string;          // the feed's id — realtime speaks in these
  trip: number;            // the pack's id
  stopId: string;
  stopSequence: number;
  scheduled: number;       // epoch seconds
  headsign: string | null;
  routeId: string;
  route: string | null;
  routeDesc: string | null;   // the long name, when the short one is shown
  routeType: number | null;
  routeColor: string | null;
  platform: string | null;
}

/** Service ids running on a date: the calendar, then its exceptions. */
export function activeServiceIds(db: Db, ymd: string, tz: string): Set<string> {
  const col = weekdayColumn(ymd, tz);
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
  return ids;
}

// TfNSW timetables non-revenue runs as ordinary trips, on routes and with
// headsigns literally named "Out Of Service". A passenger board must not
// offer them a ride to the depot.
function outOfService(text: unknown): boolean {
  return typeof text === "string" && text.toLowerCase().includes("out of service");
}

const PLATFORM_RE = /platform\s+(\w+)/i;

export function platformLabel(code: unknown, stopName: unknown): string | null {
  const c = str(code);
  if (c) return c;
  const n = str(stopName);
  const m = n ? PLATFORM_RE.exec(n) : null;
  return m ? m[1]! : null;
}

function rowToStop(r: Row): Stop {
  return {
    stop: Number(r["stop"]),
    stopId: String(r["stop_id"]),
    name: String(r["stop_name"] ?? ""),
    lat: num(r["stop_lat"]),
    lon: num(r["stop_lon"]),
    locationType: num(r["location_type"]),
    parentStation: str(r["parent_station"]),
    platformCode: str(r["platform_code"]),
  };
}

const STOP_COLS = `stop, stop_id, stop_name, stop_lat, stop_lon,
                   location_type, parent_station, platform_code`;

export function stopByFeedId(db: Db, stopId: string): Stop | null {
  const r = db.get(`SELECT ${STOP_COLS} FROM stops WHERE stop_id = ?`, [stopId]);
  return r ? rowToStop(r) : null;
}

/**
 * Every platform of a station, or the stop itself.
 *
 * A rider asking for "Roma Street" means the station, and the departures
 * are recorded against its numbered platforms. location_type 1 is a parent
 * station; anything hanging off it by parent_station is one of its faces.
 */
export function stopFamily(db: Db, stop: Stop): Stop[] {
  const family = [stop];
  for (const r of db.all(
    `SELECT ${STOP_COLS} FROM stops WHERE parent_station = ?`, [stop.stopId])) {
    family.push(rowToStop(r));
  }
  return family;
}

/**
 * The scheduled departures at a set of stops on one service date.
 *
 * Every row the timetable holds, unfiltered by time — deciding which of
 * them are near enough to show is the board's job, and it needs the ones
 * just gone as well as the ones to come.
 */
export function scheduledDepartures(
  db: Db, stops: number[], ymd: string, tz: string,
): Departure[] {
  const services = activeServiceIds(db, ymd, tz);
  if (!services.size || !stops.length) return [];
  const svc = [...services];

  const rows = db.all(
    `SELECT st.trip, st.seq, st.dep, st.stop,
            s.stop_id, s.platform_code, s.stop_name,
            t.trip_id, t.trip_headsign,
            r.route_id, r.route_short_name, r.route_long_name,
            r.route_type, r.route_color
       FROM stop_times st
       JOIN trips  t ON t.trip = st.trip
       JOIN routes r ON r.route_id = t.route_id
       JOIN stops  s ON s.stop = st.stop
      WHERE st.stop IN (${marks(stops.length)})
        AND t.service_id IN (${marks(svc.length)})`,
    [...stops, ...svc]);

  const out: Departure[] = [];
  for (const r of rows) {
    if (outOfService(r["trip_headsign"]) || outOfService(r["route_short_name"])
        || outOfService(r["route_long_name"])) continue;
    const dep = num(r["dep"]);
    if (dep === null) continue;
    out.push({
      tripId: String(r["trip_id"]),
      trip: Number(r["trip"]),
      stopId: String(r["stop_id"]),
      stopSequence: Number(r["seq"]),
      scheduled: serviceEpoch(ymd, dep, tz),
      headsign: str(r["trip_headsign"]),
      routeId: String(r["route_id"]),
      route: str(r["route_short_name"]) || str(r["route_long_name"]),
      routeDesc: str(r["route_short_name"]) ? str(r["route_long_name"]) : null,
      routeType: num(r["route_type"]),
      routeColor: str(r["route_color"]),
      platform: platformLabel(r["platform_code"], r["stop_name"]),
    });
  }
  return out;
}

/** Every call of a trip, in order — the timetable slice behind a leg. */
export function tripStops(db: Db, tripId: string): Array<{
  stopId: string; name: string; seq: number;
  arr: number | null; dep: number | null; lat: number | null; lon: number | null;
}> {
  return db.all(
    `SELECT s.stop_id, s.stop_name, st.seq, st.arr, st.dep, s.stop_lat, s.stop_lon
       FROM stop_times st
       JOIN trips t ON t.trip = st.trip
       JOIN stops s ON s.stop = st.stop
      WHERE t.trip_id = ? ORDER BY st.seq`, [tripId])
    .map((r) => ({
      stopId: String(r["stop_id"]),
      name: String(r["stop_name"] ?? ""),
      seq: Number(r["seq"]),
      arr: num(r["arr"]),
      dep: num(r["dep"]),
      lat: num(r["stop_lat"]),
      lon: num(r["stop_lon"]),
    }));
}

/** The drawn line of a trip's route. */
export function tripShape(db: Db, tripId: string): Array<[number, number]> {
  return db.all(
    `SELECT sh.lon, sh.lat FROM shapes sh
       JOIN trips t ON t.shape = sh.shape
      WHERE t.trip_id = ? ORDER BY sh.seq`, [tripId])
    .map((r) => [Number(r["lon"]), Number(r["lat"])] as [number, number]);
}
