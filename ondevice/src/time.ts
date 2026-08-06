// Service days, and turning a timetable offset into a moment in time.
//
// GTFS counts from the service day's midnight IN THE NETWORK'S OWN ZONE,
// and it counts past 24:00:00 rather than rolling over — a 25:10 departure
// belongs to the previous day's service, which is exactly how a rider
// thinks about the last train home. The pack stores those as plain seconds
// (build_pack.py), so the only hard part left is finding the right
// midnight: Brisbane never moves, but Melbourne's does twice a year, and a
// phone in Brisbane looking at Melbourne must use Melbourne's.

/** The wall clock a zone shows at an instant, encoded as a UTC timestamp. */
function wallClockAt(instant: number, tz: string): number {
  // Intl is the only thing in the platform that knows the rules; format the
  // instant in the zone and read the clock face back.
  const f = new Intl.DateTimeFormat("en-US", {
    timeZone: tz, hour12: false,
    year: "numeric", month: "2-digit", day: "2-digit",
    hour: "2-digit", minute: "2-digit", second: "2-digit",
  });
  const p: Record<string, string> = {};
  for (const part of f.formatToParts(new Date(instant))) p[part.type] = part.value;
  // "24" is how en-US hour12:false spells midnight in some engines.
  const hour = p.hour === "24" ? "00" : p.hour;
  return Date.UTC(Number(p.year), Number(p.month) - 1, Number(p.day),
                  Number(hour), Number(p.minute), Number(p.second));
}

/**
 * The instant at which a zone's clocks read a given wall time.
 *
 * Guess that the instant is the same number read as UTC, measure the
 * zone's offset there and correct — then check the correction actually
 * lands on the wall clock asked for, because on a DST boundary the offset
 * at the guess and the offset at the answer are different, and correcting
 * blindly a second time walks straight past it. (That bug put every
 * Melbourne departure on 4 October exactly one hour late.)
 *
 * Twice a year a wall clock is ambiguous or does not exist, and the rule
 * here matches Python's, because the server is the reference: when the
 * clock reads a time twice, take the first (fold=0, the earlier instant);
 * when it never reads it at all, extend the offset from before the jump
 * (the later instant).
 */
function wallClockToInstant(
  y: number, mo: number, d: number, hh: number, mi: number, ss: number, tz: string,
): number {
  const target = Date.UTC(y, mo - 1, d, hh, mi, ss);
  const offsetAt = (t: number) => wallClockAt(t, tz) - t;
  // Probe half a day either side, which brackets any transition near the
  // target, and try BOTH offsets. Deriving the second candidate from the
  // first is not enough: when the clock reads a time twice, the derivation
  // can land on the same reading both times and never see the other one.
  const candidates = [...new Set([
    target - offsetAt(target - 43_200_000),
    target - offsetAt(target + 43_200_000),
  ])];
  const valid = candidates.filter((t) => wallClockAt(t, tz) === target);
  if (valid.length) return Math.min(...valid);   // ambiguous: fold=0, the first
  return Math.max(...candidates);                // nonexistent: the old offset
}

/** The instant at which a service date begins in the network's zone. */
export function zonedMidnight(ymd: string, tz: string): number {
  return wallClockToInstant(Number(ymd.slice(0, 4)), Number(ymd.slice(4, 6)),
                            Number(ymd.slice(6, 8)), 0, 0, 0, tz);
}

/**
 * Seconds-into-service-day -> epoch seconds, in the network's zone.
 *
 * The addition is WALL CLOCK, not elapsed time: GTFS counts from the
 * service day's midnight, and 25:10 means "ten past one tomorrow morning
 * by the clock on the wall", not "twenty-five hours of elapsed time
 * later". On the morning the clocks go forward those differ by an hour,
 * and the server (a tz-aware datetime plus a timedelta, which Python
 * evaluates on the clock face) does it the wall-clock way.
 */
export function serviceEpoch(ymd: string, secs: number, tz: string): number {
  const days = Math.floor(secs / 86400);
  const rem = secs - days * 86400;
  const t = wallClockToInstant(
    Number(ymd.slice(0, 4)), Number(ymd.slice(4, 6)),
    Number(ymd.slice(6, 8)) + days,          // Date.UTC rolls the month over
    Math.floor(rem / 3600), Math.floor((rem % 3600) / 60), rem % 60, tz);
  return Math.round(t / 1000);
}

/** "YYYYMMDD" for an instant, as the network's calendar sees it. */
export function serviceYmd(instant: number, tz: string): string {
  const f = new Intl.DateTimeFormat("en-CA", {
    timeZone: tz, year: "numeric", month: "2-digit", day: "2-digit",
  });
  return f.format(new Date(instant)).replace(/-/g, "");
}

const WEEKDAY_COLUMN = ["sunday", "monday", "tuesday", "wednesday",
                        "thursday", "friday", "saturday"] as const;

/** Which calendar column governs a service date. */
export function weekdayColumn(ymd: string, tz: string): string {
  // Midday avoids any question of which day a midnight instant falls on.
  const noon = zonedMidnight(ymd, tz) + 12 * 3600 * 1000;
  const name = new Intl.DateTimeFormat("en-US", { timeZone: tz, weekday: "short" })
    .format(new Date(noon));
  const idx = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"].indexOf(name);
  return WEEKDAY_COLUMN[idx === -1 ? 1 : idx]!;
}

/** The day before a service date, as "YYYYMMDD". */
export function previousYmd(ymd: string): string {
  const y = Number(ymd.slice(0, 4));
  const m = Number(ymd.slice(4, 6));
  const d = Number(ymd.slice(6, 8));
  const t = Date.UTC(y, m - 1, d) - 86400000;
  const dt = new Date(t);
  const p = (n: number) => String(n).padStart(2, "0");
  return `${dt.getUTCFullYear()}${p(dt.getUTCMonth() + 1)}${p(dt.getUTCDate())}`;
}

/**
 * The service days a board has to consider at a given moment: today, and
 * yesterday — whose after-midnight runs (24:xx and later) are still out
 * there. A 00:40 train is yesterday's service, and looking only at today
 * loses the last hour of the night.
 */
export function serviceDates(instant: number, tz: string): string[] {
  const today = serviceYmd(instant, tz);
  return [previousYmd(today), today];
}
