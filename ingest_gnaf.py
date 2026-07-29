"""
Build a slim local geocoder database from Geoscape G-NAF (the Geocoded
National Address File): every street-numbered address in Australia with
house-level coordinates, so "397 Christine Avenue" resolves to the actual
house — OpenStreetMap's residential number coverage is patchy, and Nominatim
falls back to pinning an arbitrary point along the street (see HANDOVER).

    python ingest_gnaf.py                # download the quarterly zip (~1.7 GB)
    python ingest_gnaf.py /path/to/gnaf.zip

Output: gnaf.sqlite3 (GNAF_DB env, default alongside GTFS_DB) —
    streets(id, name, type, locality, state)      ~700 k rows
    addresses(street_id, num, lat, lon)           ~14 M rows, ~1 per house
    streets_fts                                    FTS5 over streets
Built via a scratch sidecar DB (raw G-NAF is ~35 normalised tables; we join
four) and os.replace()d into position, so a rebuild is safe under a running
server. Refresh cadence: G-NAF is quarterly; addresses churn slowly — rerun
this a few times a year and re-ship (see deploy/state/*.yaml, gnaf-db).

Licence: G-NAF is open data under the Open G-NAF EULA (attribution
required; it must not be used for generating unsolicited mail).
"""

import csv
import io
import os
import sqlite3
import sys
import urllib.request
import zipfile
from pathlib import Path

# The quarterly URL rotates (month + a build number in the filename). When
# this 404s, look up the current one:
#   https://data.gov.au/data/api/3/action/package_search?q=%22g-naf%22
GNAF_URL = ("https://data.gov.au/data/dataset/19432f89-dc3a-4ef3-b943-5326ef1dbecc/"
            "resource/f8666213-4079-44da-bede-ebda3a4363e0/download/"
            "g-naf_may26_allstates_gda2020_psv_1023.zip")

BASE = Path(__file__).parent
GTFS_DB = Path(os.environ.get("GTFS_DB") or BASE / "gtfs.sqlite3")
DB_PATH = Path(os.environ.get("GNAF_DB") or GTFS_DB.parent / "gnaf.sqlite3")


def download(url: str, dest: Path) -> None:
    print(f"Downloading {url} ...")
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req) as resp, open(dest, "wb") as f:
        while chunk := resp.read(1 << 20):
            f.write(chunk)
    print(f"Saved to {dest} ({dest.stat().st_size / 1e6:.1f} MB)")


def psv_rows(zf: zipfile.ZipFile, name: str):
    """Stream one PSV member as dicts (pipe-separated, header row first)."""
    with zf.open(name) as raw:
        text = io.TextIOWrapper(raw, encoding="utf-8", newline="")
        reader = csv.DictReader(text, delimiter="|")
        yield from reader


def members(zf: zipfile.ZipFile, suffix: str, exclude: str = "") -> list[str]:
    """All per-state members ending like _ADDRESS_DETAIL_psv.psv. `exclude`
    guards prefixes-of-suffixes: _LOCALITY also tail-matches
    STREET_LOCALITY's filenames."""
    def want(n: str) -> bool:
        u = n.upper()
        return u.endswith(suffix.upper()) and not (exclude and exclude.upper() in u)
    out = [n for n in zf.namelist()
           if want(n) and "/standard/" in n.casefold()]
    if not out:  # some releases skip the Standard/ folder level
        out = [n for n in zf.namelist() if want(n)]
    return sorted(out)


def build(zip_path: Path) -> None:
    tmp = DB_PATH.with_suffix(".sqlite3.tmp")
    scratch = DB_PATH.with_suffix(".sqlite3.scratch")
    for p in (tmp, scratch):
        p.unlink(missing_ok=True)

    con = sqlite3.connect(tmp)
    con.execute(f"ATTACH DATABASE '{scratch}' AS raw")
    con.executescript("""
        PRAGMA journal_mode=OFF;  PRAGMA synchronous=OFF;
        PRAGMA raw.journal_mode=OFF;  PRAGMA raw.synchronous=OFF;
        CREATE TABLE raw.state    (pid TEXT PRIMARY KEY, abbr TEXT);
        CREATE TABLE raw.locality (pid TEXT PRIMARY KEY, name TEXT, state_pid TEXT);
        CREATE TABLE raw.street   (pid TEXT PRIMARY KEY, name TEXT, type TEXT, loc_pid TEXT);
        CREATE TABLE raw.geo      (pid TEXT PRIMARY KEY, lat REAL, lon REAL);
        CREATE TABLE raw.addr     (pid TEXT, num INTEGER, street_pid TEXT);
        CREATE TABLE streets (
            id INTEGER PRIMARY KEY, name TEXT, type TEXT, locality TEXT, state TEXT);
        CREATE TABLE addresses (
            street_id INTEGER, num INTEGER, lat REAL, lon REAL,
            PRIMARY KEY (street_id, num)) WITHOUT ROWID;
        CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
    """)

    def load(suffix, sql, mapper, label, exclude=""):
        total = 0
        with zipfile.ZipFile(zip_path) as zf:
            for name in members(zf, suffix, exclude):
                batch = []
                for r in psv_rows(zf, name):
                    if r.get("DATE_RETIRED"):
                        continue
                    row = mapper(r)
                    if row:
                        batch.append(row)
                    if len(batch) >= 50_000:
                        con.executemany(sql, batch); total += len(batch); batch = []
                con.executemany(sql, batch); total += len(batch)
        con.commit()
        print(f"  {label}: {total:,} rows")

    print("Loading raw G-NAF tables (streaming from the zip)...")
    load("_STATE_psv.psv",
         "INSERT OR IGNORE INTO raw.state VALUES (?,?)",
         lambda r: (r["STATE_PID"], r["STATE_ABBREVIATION"]), "states")
    load("_LOCALITY_psv.psv",
         "INSERT OR IGNORE INTO raw.locality VALUES (?,?,?)",
         lambda r: (r["LOCALITY_PID"], r["LOCALITY_NAME"], r["STATE_PID"]),
         "localities", exclude="STREET_LOCALITY")
    load("_STREET_LOCALITY_psv.psv",
         "INSERT OR IGNORE INTO raw.street VALUES (?,?,?,?)",
         lambda r: (r["STREET_LOCALITY_PID"], r["STREET_NAME"],
                    r["STREET_TYPE_CODE"] or "", r["LOCALITY_PID"]),
         "streets")
    load("_ADDRESS_DEFAULT_GEOCODE_psv.psv",
         "INSERT OR IGNORE INTO raw.geo VALUES (?,?,?)",
         lambda r: (r["ADDRESS_DETAIL_PID"],
                    float(r["LATITUDE"]), float(r["LONGITUDE"]))
                   if r["LATITUDE"] and r["LONGITUDE"] else None,
         "geocodes")
    load("_ADDRESS_DETAIL_psv.psv",
         "INSERT INTO raw.addr VALUES (?,?,?)",
         lambda r: (r["ADDRESS_DETAIL_PID"], int(r["NUMBER_FIRST"]),
                    r["STREET_LOCALITY_PID"])
                   if r["NUMBER_FIRST"] and r["NUMBER_FIRST"].isdigit() else None,
         "numbered addresses")

    print("Joining into the slim tables...")
    con.executescript("""
        INSERT INTO streets (name, type, locality, state)
            SELECT DISTINCT s.name, s.type, l.name, st.abbr
            FROM raw.street s
            JOIN raw.locality l ON l.pid = s.loc_pid
            JOIN raw.state st   ON st.pid = l.state_pid;
        -- the street_map join looks 700k street rows back up by this tuple
        CREATE INDEX street_lookup ON streets (name, type, locality, state);
        CREATE TABLE raw.street_map (pid TEXT PRIMARY KEY, street_id INTEGER);
        INSERT INTO raw.street_map
            SELECT s.pid, t.id
            FROM raw.street s
            JOIN raw.locality l ON l.pid = s.loc_pid
            JOIN raw.state st   ON st.pid = l.state_pid
            JOIN streets t ON t.name = s.name AND t.type = s.type
                          AND t.locality = l.name AND t.state = st.abbr;
        INSERT OR IGNORE INTO addresses
            SELECT m.street_id, a.num, g.lat, g.lon
            FROM raw.addr a
            JOIN raw.street_map m ON m.pid = a.street_pid
            JOIN raw.geo g        ON g.pid = a.pid;
    """)
    con.execute("INSERT INTO meta VALUES ('source', ?)", (zip_path.name,))

    print("Building the street FTS index...")
    con.executescript("""
        CREATE VIRTUAL TABLE streets_fts USING fts5(
            name, type, locality, state, content='streets', content_rowid='id');
        INSERT INTO streets_fts (rowid, name, type, locality, state)
            SELECT id, name, type, locality, state FROM streets;
    """)
    con.execute("DROP INDEX street_lookup")   # FTS serves queries; save the MBs
    n_str = con.execute("SELECT COUNT(*) FROM streets").fetchone()[0]
    n_adr = con.execute("SELECT COUNT(*) FROM addresses").fetchone()[0]
    con.commit()
    con.execute("DETACH DATABASE raw")
    con.close()
    scratch.unlink(missing_ok=True)

    os.replace(tmp, DB_PATH)
    print(f"Done. {n_str:,} streets / {n_adr:,} addresses "
          f"in {DB_PATH} ({DB_PATH.stat().st_size / 1e6:.0f} MB)")


if __name__ == "__main__":
    arg = sys.argv[1] if len(sys.argv) > 1 else None
    if arg and Path(arg).exists():
        zip_path = Path(arg)
    else:
        zip_path = DB_PATH.parent / "gnaf.zip"
        download(arg or GNAF_URL, zip_path)
    build(zip_path)
