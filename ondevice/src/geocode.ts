// Turning text into places, and places back into text.
//
// A port of geocoding.py's local half: G-NAF house numbers and the named
// places index. Nominatim is NOT ported — it is a network call to someone
// else's server, which is the whole thing we are removing. On a device the
// two local indexes are the answer or there is no answer, and both cover
// Australia comprehensively enough that the fallback was already rare.
//
// The pack stores coordinates as micro-degrees and replaces the server's
// spatial index with cell ranges (see build_pack.py), so proximity here is
// an id-range scan rather than a B-tree probe. Same answers, different
// route to them.

import { type Db, coordScale, marks, num } from "./db.ts";

export interface Found {
  label: string;
  lat: number;
  lon: number;
}

const CELL_DEG = 0.01;               // must match build_pack.py's grid

const cellKey = (lat: number, lon: number): number =>
  Math.floor((lat + 90) / CELL_DEG) * 40000 + Math.floor((lon + 180) / CELL_DEG);

/** The id ranges covering a square of cells around a point. */
function cellRanges(db: Db, lat: number, lon: number, rings: number)
    : Array<[number, number]> {
  const base = cellKey(lat, lon);
  const cells: number[] = [];
  for (let dy = -rings; dy <= rings; dy++) {
    for (let dx = -rings; dx <= rings; dx++) cells.push(base + dy * 40000 + dx);
  }
  return db.all(`SELECT lo, hi FROM cells WHERE cell IN (${marks(cells.length)})`,
                cells)
    .map((r) => [Number(r["lo"]), Number(r["hi"])] as [number, number]);
}

const titleCase = (s: string): string =>
  s.toLowerCase().replace(/(^|[\s'-])([a-z0-9])/g, (_m, p, c) => p + c.toUpperCase());

// Metres per degree, flat-earth. The server uses these exact constants and
// the ranking must agree with it, so they are copied rather than improved.
const M_PER_DEG_LAT = 111000.0;
const M_PER_DEG_LON = 88000.0;

const STREET_ABBR: Record<string, string> = {
  st: "street", rd: "road", ave: "avenue", av: "avenue", dr: "drive",
  drv: "drive", ct: "court", crt: "court", cres: "crescent", cr: "crescent",
  pde: "parade", hwy: "highway", tce: "terrace", pl: "place",
  blvd: "boulevard", bvd: "boulevard", ln: "lane", cct: "circuit",
  esp: "esplanade", gr: "grove", gdns: "gardens", pkwy: "parkway",
};

/**
 * House-number lookup.
 *
 * Returns null for "not applicable" — no query leading with a number —
 * exactly as the server does, so the caller can fall through to places.
 * An address short of its exact number gives the NEAREST number on that
 * street, honestly labelled: next door beats a random point mid-road.
 */
export function gnafGeocode(
  db: Db, q: string, stateBias: string, limit = 8,
  near?: { lat: number; lon: number } | null,
): Found[] | null {
  const m = /^\s*(?:\d+\s*\/\s*)?(\d+)\s+(.+)/.exec(q);
  if (!m) return null;
  const wanted = Number(m[1]);
  const tokens = (m[2]!.toLowerCase().match(/[a-z']+/g) ?? [])
    .map((t) => STREET_ABBR[t] ?? t);
  if (!tokens.length) return null;

  const addrScale = coordScale(db);
  const fts = tokens.map((t) => `"${t}"`).join(" ");
  let streets;
  try {
    streets = db.all(
      `SELECT s.id, s.name, s.type, s.locality, s.state
         FROM streets_fts f JOIN streets s ON s.id = f.rowid
        WHERE streets_fts MATCH ?
        ORDER BY (s.state = ?) DESC, bm25(streets_fts) LIMIT 150`,
      [fts, stateBias]);
  } catch {
    return null;                     // a query string FTS cannot parse
  }

  const out: Array<Found & { exact: boolean }> = [];
  for (const s of streets) {
    const a = db.get(
      `SELECT num, lat, lon FROM addresses WHERE street_id = ?
        ORDER BY ABS(num - ?) LIMIT 1`, [Number(s["id"]), wanted]);
    if (!a) continue;
    const n = Number(a["num"]);
    let title = `${n} ${titleCase(String(s["name"]))}`;
    if (s["type"]) title += ` ${titleCase(String(s["type"]))}`;
    out.push({
      label: `${title}, ${titleCase(String(s["locality"]))} ${s["state"]}`,
      lat: Number(a["lat"]) / addrScale,
      lon: Number(a["lon"]) / addrScale,
      exact: n === wanted,
    });
  }

  // Rank the WHOLE pool before trimming. With a bias point, being the exact
  // number is worth kilometres but not everything: someone near Toowong
  // typing "12 High Street" means the High Street there, but an exact 397 in
  // the next suburb must still beat a 480 marginally closer.
  if (near) {
    const coslat = Math.cos((near.lat * Math.PI) / 180) || 1e-9;
    const km = (r: Found) => Math.sqrt(
      (r.lat - near.lat) ** 2 + ((r.lon - near.lon) * coslat) ** 2) * 111.0;
    out.sort((a, b) => (km(a) + (a.exact ? 0 : 25)) - (km(b) + (b.exact ? 0 : 25)));
  } else {
    out.sort((a, b) => Number(a.exact ? 0 : 1) - Number(b.exact ? 0 : 1));
  }
  const trimmed = out.slice(0, limit).map(({ label, lat, lon }) => ({ label, lat, lon }));
  return trimmed.length ? trimmed : null;
}

/**
 * Named places: "bunnings burleigh".
 *
 * Every token must match; the last one as a prefix, so it behaves like
 * search-as-you-type. Inside the viewbox ranks first, then an exact name
 * match ("Pacific Fair" the mall above "Roll'd Pacific Fair" the tenant),
 * then nearest.
 */
export function placesGeocode(
  db: Db, q: string, viewbox: [number, number, number, number], limit = 8,
  near?: { lat: number; lon: number } | null,
): Found[] | null {
  const tokens = q.toLowerCase().match(/[a-z0-9']+/g) ?? [];
  if (!tokens.length || q.trim().length < 3) return null;
  const head = tokens.slice(0, -1).map((t) => `"${t}"`).join(" ");
  const fts = (head ? head + " " : "") + `"${tokens[tokens.length - 1]}"*`;

  let rows;
  try {
    rows = db.all(
      `SELECT p.name, p.category, p.suburb, p.state, p.lat, p.lon
         FROM places_fts f JOIN places p ON p.id = f.rowid
        WHERE places_fts MATCH ? ORDER BY bm25(places_fts) LIMIT 250`, [fts]);
  } catch {
    return null;
  }
  if (!rows.length) return null;

  const scale = coordScale(db);
  const [lon1, lat1, lon2, lat2] = viewbox;
  const coslat = near ? (Math.cos((near.lat * Math.PI) / 180) || 1e-9) : 1;
  const qnorm = tokens.join("");
  const scored = rows.map((r) => {
    const lat = Number(r["lat"]) / scale, lon = Number(r["lon"]) / scale;
    const inBox = lat >= Math.min(lat1, lat2) && lat <= Math.max(lat1, lat2)
               && lon >= Math.min(lon1, lon2) && lon <= Math.max(lon1, lon2);
    const exact = String(r["name"]).toLowerCase().replace(/[^a-z0-9]/g, "") === qnorm;
    const d = near ? (lat - near.lat) ** 2 + ((lon - near.lon) * coslat) ** 2 : 0;
    return { r, lat, lon, inBox, exact, d };
  });
  scored.sort((a, b) =>
    Number(b.inBox) - Number(a.inBox) || Number(b.exact) - Number(a.exact) || a.d - b.d);

  const out: Found[] = [];
  const seen = new Set<string>();
  for (const s of scored) {
    const bits = [String(s.r["category"] ?? "")];
    if (s.r["suburb"]) bits.push(`${s.r["suburb"]} ${s.r["state"] ?? ""}`.trim());
    const label = `${s.r["name"]} — ${bits.join(", ")}`;
    if (seen.has(label)) continue;
    seen.add(label);
    out.push({ label, lat: s.lat, lon: s.lon });
    if (out.length >= limit) break;
  }
  return out.length ? out : null;
}

/**
 * A point -> the address a person would say.
 *
 * Nearest address wins; past ~150 m the answer is hedged ("near ...")
 * because the house number would be a lie, and with nothing within ~600 m
 * there is honestly no address to give.
 */
export function reverseGeocode(db: Db, lat: number, lon: number)
    : { label: string | null; exact?: boolean } {
  const scale = coordScale(db);
  const coslat = Math.max(0.2, Math.cos((lat * Math.PI) / 180));
  for (const [spanM, hedge] of [[150, ""], [600, "near "]] as const) {
    // One cell is ~1.1 km, so one ring covers both spans comfortably.
    const ranges = cellRanges(db, lat, lon, 1);
    if (!ranges.length) continue;
    const dlat = spanM / M_PER_DEG_LAT;
    const dlon = spanM / (M_PER_DEG_LAT * coslat);
    const where = ranges.map(() => "(a.id BETWEEN ? AND ?)").join(" OR ");
    const rows = db.all(
      `SELECT a.num, a.lat, a.lon, s.name, s.type, s.locality, s.state
         FROM addresses a JOIN streets s ON s.id = a.street_id
        WHERE (${where})
          AND a.lat BETWEEN ? AND ? AND a.lon BETWEEN ? AND ?`,
      [...ranges.flat(),
       Math.round((lat - dlat) * scale), Math.round((lat + dlat) * scale),
       Math.round((lon - dlon) * scale), Math.round((lon + dlon) * scale)]);

    let best: typeof rows[number] | null = null;
    let bd = spanM * spanM;
    for (const r of rows) {
      const ala = Number(r["lat"]) / scale, alo = Number(r["lon"]) / scale;
      const d2 = ((ala - lat) * M_PER_DEG_LAT) ** 2
               + ((alo - lon) * M_PER_DEG_LAT * Math.cos((lat * Math.PI) / 180)) ** 2;
      if (d2 < bd) { bd = d2; best = r; }
    }
    if (best) {
      const street = titleCase(String(best["name"]))
        + (best["type"] ? ` ${titleCase(String(best["type"]))}` : "");
      const head = hedge ? street : `${num(best["num"])} ${street}`;
      return {
        label: `${hedge}${head}, ${titleCase(String(best["locality"]))} ${best["state"]}`,
        exact: !hedge,
      };
    }
  }
  return { label: null };
}

// Labels a caller may send that name nothing. A shared URL carrying one
// should still produce a journey a rider can follow.
const PLACEHOLDER = new Set(["", "dropped pin", "destination", "your address",
                             "your destination", "near me", "your location", "you"]);

export function pointLabel(
  db: Db, lat: number, lon: number, given: string | null, fallback: string,
): string {
  if (given && !PLACEHOLDER.has(given.trim().toLowerCase())) return given;
  return reverseGeocode(db, lat, lon).label ?? fallback;
}
