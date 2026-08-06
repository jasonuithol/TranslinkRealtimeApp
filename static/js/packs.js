// region packs: what the app downloads so it can answer offline
// Split from index.html; these files load in order as classic
// scripts and share one global scope — boot.js must stay last.
  // --- packs ---------------------------------------------------------------
  // A pack is one region's data — timetable, walking graph, addresses,
  // places, basemap — sliced out of the national databases by build_pack.py
  // and served as flat assets on a GitHub release. Nothing here talks to the
  // VPS: the index says which regions exist and where they are, and each
  // region's manifest says what its files are, how big, and their hashes.
  //
  // Four files rather than one because they age differently. The timetable
  // is reissued weekly; the streets and the basemap almost never change. So
  // the first install is a big download the rider agrees to, and every later
  // one is a quiet timetable refresh they can wave off.

  // A GitHub release is a flat namespace, so an asset is "<region>-<file>".
  // ?packbase= points the whole thing at a local server for testing.
  //
  // Measured 2026-08-06: release assets serve ranges but send NO
  // Access-Control-Allow-Origin. That is fine for the packaged app, whose
  // downloads go through the native filesystem plugin and never meet CORS
  // — and it means the browser's OPFS path below can only reach a mirror
  // that does send the header. A browser that cannot fetch simply gets no
  // packs and stays on the server, which is what the free web version is.
  const PACK_BASE = (new URLSearchParams(location.search).get("packbase")
    || "https://github.com/jasonuithol/TranslinkRealtimeApp"
       + "/releases/download/packs-latest").replace(/\/$/, "");
  const packUrl = (region, file) => `${PACK_BASE}/${region}-${file}`;

  // The timetable is the only file that expires; the rest are geography.
  const PACK_TIMETABLE = "timetable.sqlite3";

  function packSay(bytes) {
    if (bytes >= 1e9) return `${(bytes / 1e9).toFixed(1)} GB`;
    if (bytes >= 1e6) return `${Math.round(bytes / 1e6)} MB`;
    return `${Math.round(bytes / 1e3)} kB`;
  }

  // --- where the bytes live ------------------------------------------------
  // On a phone the pack goes to the app's own data directory, where a native
  // SQLite can open it. In a browser it goes to the origin-private file
  // system, which is the only place a few hundred megabytes may live. A
  // browser with neither has no pack support at all, and the app stays on
  // the server — which is exactly what the free web version is.
  const packStore = (() => {
    const cap = window.Capacitor && window.Capacitor.Plugins
             && window.Capacitor.Plugins.Filesystem;
    if (cap) {
      return {
        kind: "device",
        // Filesystem.downloadFile writes natively, without the file ever
        // passing through JavaScript — the only sane way to move 100 MB on
        // a phone. Progress arrives as events rather than as a stream.
        async fetchTo(region, file, url, { onBytes, signal }) {
          const path = `packs/${region}/${file}`;
          let off = null;
          if (window.Capacitor.Plugins.Filesystem.addListener) {
            off = await window.Capacitor.Plugins.Filesystem.addListener(
              "progress", (e) => onBytes(e.bytes, e.contentLength));
          }
          try {
            if (signal && signal.aborted) throw new DOMException("", "AbortError");
            await cap.downloadFile({
              url, path, directory: "DATA", recursive: true, progress: true });
          } finally {
            if (off && off.remove) await off.remove();
          }
          return null;                  // hashing is the platform's problem
        },
        async remove(region, file) {
          try {
            await cap.deleteFile({ path: `packs/${region}/${file}`,
                                   directory: "DATA" });
          } catch { /* never installed, or already gone */ }
        },
      };
    }
    if (navigator.storage && navigator.storage.getDirectory) {
      return {
        kind: "opfs",
        async fetchTo(region, file, url, { onBytes, signal }) {
          const root = await navigator.storage.getDirectory();
          const dir = await root.getDirectoryHandle("packs", { create: true });
          const rdir = await dir.getDirectoryHandle(region, { create: true });
          const fh = await rdir.getFileHandle(file, { create: true });
          const out = await fh.createWritable();
          const res = await fetch(url, { signal });
          if (!res.ok) { await out.abort(); throw new Error(`HTTP ${res.status}`); }
          const total = Number(res.headers.get("content-length")) || 0;
          const hash = sha256Stream();
          let got = 0;
          try {
            const reader = res.body.getReader();
            for (;;) {
              const { done, value } = await reader.read();
              if (done) break;
              await out.write(value);
              hash.update(value);
              got += value.length;
              onBytes(got, total);
            }
          } catch (e) {
            await out.abort();
            // A cancelled write leaves a half file; it must not look installed.
            try { await rdir.removeEntry(file); } catch { /* nothing to undo */ }
            throw e;
          }
          await out.close();
          return hash.hex();
        },
        async remove(region, file) {
          try {
            const root = await navigator.storage.getDirectory();
            const dir = await root.getDirectoryHandle("packs");
            const rdir = await dir.getDirectoryHandle(region);
            await rdir.removeEntry(file);
          } catch { /* never installed, or already gone */ }
        },
      };
    }
    return null;                     // server mode; packs are not offered
  })();

  // Offered in the packaged app, and to a browser only when ?packbase=
  // points it somewhere on purpose. The free web version talks to the
  // server: a browser cannot reach the release assets (no CORS) and has no
  // engine to read a pack with yet, so downloading one would spend a
  // rider's disk on nothing — and asking github.com on every cold start
  // for an answer we would throw away only slows the page down.
  // Read ONCE, at load. The planner rewrites the query string on every pin
  // move (syncPlanUrl), and ?packbase= is not among the parameters it
  // keeps — so re-reading location.search here meant packs quietly switched
  // themselves off the moment a goose was dragged, which is precisely when
  // the next question needed asking.
  const PACK_BASE_GIVEN = new URLSearchParams(location.search).has("packbase");
  const packsSupported = () => packStore !== null
    && (Boolean(window.Capacitor) || PACK_BASE_GIVEN);

  // --- what is installed ---------------------------------------------------
  // The record of an install is the manifest entry of every file that
  // actually landed. Comparing it to a fresh manifest says precisely what
  // has changed — and after a cancelled download, what did not.
  const packKey = (region) => `pack:${region}`;
  function packLocal(region) {
    try { return JSON.parse(localStorage.getItem(packKey(region))) || null; }
    catch { return null; }
  }
  // The human name of the region, kept with the record so the settings
  // panel can say "Translink · South East Queensland" rather than "seq"
  // without needing the network to tell it.
  function packSaveName(region, name) {
    const rec = packLocal(region) || { files: {} };
    rec.name = name;
    localStorage.setItem(packKey(region), JSON.stringify(rec));
  }

  function packSaveFile(region, file, entry) {
    const rec = packLocal(region) || { files: {} };
    rec.files[file] = entry;
    rec.at = Date.now();
    localStorage.setItem(packKey(region), JSON.stringify(rec));
  }

  let packIndexCache = null;
  async function packIndex() {
    if (packIndexCache) return packIndexCache;
    const res = await fetch(`${PACK_BASE}/index.json`, { cache: "no-cache" });
    if (!res.ok) throw new Error(`pack index: HTTP ${res.status}`);
    packIndexCache = await res.json();
    return packIndexCache;
  }

  // Memoised for the session: dragging a goose asks this question on
  // every drop, and the common answer — "you already have this region" —
  // must not cost a round trip each time a pin is nudged.
  const packManifestCache = {};
  async function packManifest(region) {
    if (packManifestCache[region]) return packManifestCache[region];
    const res = await fetch(packUrl(region, "manifest.json"),
                            { cache: "no-cache" });
    if (!res.ok) throw new Error(`manifest: HTTP ${res.status}`);
    packManifestCache[region] = await res.json();
    return packManifestCache[region];
  }

  // Which region covers a point. The bbox is the honest answer — it is the
  // area the pack was built for. Falling back to the nearest centre keeps
  // somewhere out past the edge of every pack from being nowhere at all.
  function packRegionFor(lat, lon, index) {
    const rs = (index && index.regions) || [];
    const inside = rs.filter((r) => r.bbox
      && lat >= r.bbox[0] && lat <= r.bbox[1]
      && lon >= r.bbox[2] && lon <= r.bbox[3]);
    if (inside.length === 1) return inside[0];
    // Overlapping packs (Brisbane sits inside regional Queensland's box):
    // the one whose centre is closest is the one you are actually in.
    const pool = inside.length ? inside : rs;
    let best = null, bestD = Infinity;
    for (const r of pool) {
      if (!r.center) continue;
      const d = haversineKm(lat, lon, r.center[1], r.center[0]);
      if (d < bestD) { best = r; bestD = d; }
    }
    return best;
  }

  // What needs downloading: everything for a first install, or just the
  // files whose hash has moved for an update.
  function packPlan(local, manifest) {
    const files = [];
    for (const [name, entry] of Object.entries(manifest.files || {})) {
      const have = local && local.files && local.files[name];
      if (!have || have.sha256 !== entry.sha256) files.push({ name, ...entry });
    }
    const bytes = files.reduce((n, f) => n + f.bytes, 0);
    // "Install" means some file has never arrived — including the case of a
    // first attempt the rider cancelled half way. Resuming those remaining
    // hundreds of megabytes is still a decision for them to make, so it
    // asks again (for what is left, not for the whole pack). "Update" is
    // only ever files they already hold, in a newer edition.
    const missing = Object.keys(manifest.files || {}).some(
      (name) => !(local && local.files && local.files[name]));
    const kind = missing ? "install" : (files.length ? "update" : "current");
    // An update that is only the timetable is the weekly refresh, and needs
    // no permission — it replaces data the rider already agreed to hold.
    const timetableOnly = kind === "update"
      && files.every((f) => f.name === PACK_TIMETABLE);
    return { kind, files, bytes, timetableOnly };
  }

  // A file is downloaded into whichever of its two slots is not currently
  // live, and only becomes live once it has arrived whole. Cancelling a
  // weekly timetable refresh half way must leave last week's timetable
  // working, not a truncated file with the right name — and neither store
  // here offers an atomic rename to lean on.
  const otherSlot = (slot) => (slot === "b" ? "a" : "b");
  const slotName = (file, slot) =>
    (slot === "b" ? file.replace(/(\.[^.]*)$/, ".b$1") : file);

  // Run a plan. Progress is reported as one fraction across the whole
  // download, not per file — a rider watching a bar wants to know how much
  // longer, not which of four files is in flight.
  async function packRun(region, plan, { onProgress, signal }) {
    const total = plan.bytes || 1;
    let done = 0;
    for (const f of plan.files) {
      if (signal && signal.aborted) throw new DOMException("cancelled", "AbortError");
      const live = ((packLocal(region) || {}).files || {})[f.name];
      const slot = otherSlot(live && live.slot);
      const stored = slotName(f.name, slot);
      let last = 0;
      const hash = await packStore.fetchTo(region, stored, packUrl(region, f.name), {
        signal,
        onBytes: (got) => {
          done += got - last;
          last = got;
          onProgress(Math.min(done / total, 1), f.name);
        },
      });
      // Size is checked always; the hash whenever the platform handed us
      // the bytes to check it with. A truncated file that still looks
      // installed is the one failure that would be silent forever.
      if (hash && hash !== f.sha256) {
        await packStore.remove(region, stored);
        throw new Error(`${f.name} arrived damaged — not installed`);
      }
      done += f.bytes - last;              // trust the manifest for the tail
      // The swap: the new slot goes live, then last week's copy is dropped.
      packSaveFile(region, f.name,
                   { sha256: f.sha256, bytes: f.bytes, slot, stored });
      if (live && live.stored && live.stored !== stored) {
        await packStore.remove(region, live.stored);
      }
      onProgress(Math.min(done / total, 1), f.name);
    }
  }

  // --- the gate ------------------------------------------------------------
  // Called once, on a cold start, as soon as we know where the rider is.
  // First visit to a region: ask, because it is a large download on what
  // may be mobile data. Every visit after: refresh the timetable without
  // asking, because it replaces data they already said yes to — but let
  // them wave it off while it runs.
  const $pk = (id) => document.getElementById(id);
  // Where the sheet is being shown. At startup it belongs to the boot
  // screen, which stays up afterwards while the app finishes starting.
  // Mid-session it borrows the same full-screen container as a scrim
  // over the map, and must put it away again when it is done.
  let packOverMap = false;

  function packShow(title, body) {
    if (packOverMap) {
      $pk("boot").hidden = false;
      $pk("boot").classList.add("sheet-only");
      // A toolbar tip left over from a moment ago has nothing to teach
      // across a modal question. New ones are already suppressed while
      // this is up; this clears the one that was mid-sentence.
      if (typeof coachClear === "function") coachClear();
    }
    $pk("pk").hidden = false;
    $pk("boot-say").hidden = true;   // the sheet speaks for itself now
    $pk("pk-title").textContent = title;
    $pk("pk-body").textContent = body;
    $pk("pk-say").textContent = "";
    $pk("pk-btns").innerHTML = "";
  }
  function packHide() {
    $pk("pk").hidden = true;
    $pk("boot-say").hidden = false;
    if (packOverMap) {
      $pk("boot").classList.remove("sheet-only");
      $pk("boot").hidden = true;      // give the map back
    }
  }

  function packButton(label, cls, onClick) {
    const b = document.createElement("button");
    b.type = "button";
    b.className = cls;
    b.textContent = label;
    b.addEventListener("click", onClick);
    $pk("pk-btns").appendChild(b);
    return b;
  }

  function packMeter(fraction) {
    const m = $pk("pk-meter");
    m.hidden = false;
    m.style.setProperty("--p", String(fraction));
  }

  // First install: the rider decides. The size is the whole point of asking.
  function packAsk(region, plan) {
    return new Promise((resolve) => {
      packShow(region.name,
        "Keep this region on your phone and it works without a signal — "
        + `timetables, streets and the map. ${packSay(plan.bytes)}, once.`);
      packButton(`Download ${packSay(plan.bytes)}`, "pk-go",
                 () => resolve(true));
      packButton("Not now", "pk-alt", () => resolve(false));
    });
  }

  // The download itself, install or update. Resolves either way: a cancelled
  // or failed download is not a reason to refuse to start the app.
  async function packDownload(region, plan, { overMap = true } = {}) {
    packOverMap = overMap;
    const ctl = new AbortController();
    packShow(region.name, plan.kind === "install"
      ? `Downloading ${packSay(plan.bytes)}…`
      : `Updating the timetable — ${packSay(plan.bytes)}.`);
    packMeter(0);
    const say = $pk("pk-say");
    say.textContent = `0%  ·  0 MB of ${packSay(plan.bytes)}`;
    packButton("Cancel", "pk-alt", () => ctl.abort());
    let shown = -1;
    try {
      await packRun(region.id, plan, {
        signal: ctl.signal,
        onProgress: (p) => {
          packMeter(p);
          const pct = Math.round(p * 100);
          if (pct !== shown) {           // the goose moves; the text follows
            shown = pct;
            say.textContent = `${pct}%  ·  ${packSay(Math.round(p * plan.bytes))}`
                            + ` of ${packSay(plan.bytes)}`;
          }
        },
      });
      packMeter(1);
      say.textContent = "Ready.";
    } catch (err) {
      const cancelled = err && err.name === "AbortError";
      say.textContent = cancelled
        ? "Stopped. Your existing data is unchanged."
        : `Could not download: ${err.message}`;
      // Let the message be read, but do not make anyone tap past it.
      await new Promise((r) => setTimeout(r, cancelled ? 900 : 2200));
    }
    packHide();
  }

  // Returns the region the rider is in (whether or not its pack installed),
  // or null when packs do not apply — a browser with nowhere to put them,
  // or no published packs yet, both of which mean "carry on using the
  // server". Never throws: a pack problem must not block the app.
  async function packGate(lat, lon, { overMap = false } = {}) {
    if (!packsSupported()) return null;
    packOverMap = overMap;
    let region, manifest;
    try {
      region = packRegionFor(lat, lon, await packIndex());
      if (!region) return null;
      manifest = await packManifest(region.id);
    } catch {
      return null;                     // nothing published for this region yet
    }
    packSaveName(region.id, region.name);
    const plan = packPlan(packLocal(region.id), manifest);
    if (plan.kind === "current") return region;
    if (plan.kind === "install" && !(await packAsk(region, plan))) {
      packHide();
      return region;                   // declined: the app still runs online
    }
    await packDownload(region, plan);
    return region;
  }

  // --- SHA-256, incrementally ----------------------------------------------
  // WebCrypto can only digest a whole buffer, and a whole buffer here is
  // 100 MB of timetable held in RAM on a phone. This is the standard
  // algorithm over a streaming block buffer instead.
  function sha256Stream() {
    const K = new Uint32Array([
      0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5, 0x3956c25b, 0x59f111f1,
      0x923f82a4, 0xab1c5ed5, 0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3,
      0x72be5d74, 0x80deb1fe, 0x9bdc06a7, 0xc19bf174, 0xe49b69c1, 0xefbe4786,
      0x0fc19dc6, 0x240ca1cc, 0x2de92c6f, 0x4a7484aa, 0x5cb0a9dc, 0x76f988da,
      0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7, 0xc6e00bf3, 0xd5a79147,
      0x06ca6351, 0x14292967, 0x27b70a85, 0x2e1b2138, 0x4d2c6dfc, 0x53380d13,
      0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85, 0xa2bfe8a1, 0xa81a664b,
      0xc24b8b70, 0xc76c51a3, 0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070,
      0x19a4c116, 0x1e376c08, 0x2748774c, 0x34b0bcb5, 0x391c0cb3, 0x4ed8aa4a,
      0x5b9cca4f, 0x682e6ff3, 0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208,
      0x90befffa, 0xa4506ceb, 0xbef9a3f7, 0xc67178f2]);
    const h = new Uint32Array([0x6a09e667, 0xbb67ae85, 0x3c6ef372, 0xa54ff53a,
                               0x510e527f, 0x9b05688c, 0x1f83d9ab, 0x5be0cd19]);
    const w = new Uint32Array(64);
    const buf = new Uint8Array(64);
    let bufLen = 0, total = 0;
    const rotr = (x, n) => (x >>> n) | (x << (32 - n));

    function block(b, off) {
      for (let i = 0; i < 16; i++) {
        w[i] = (b[off + i * 4] << 24) | (b[off + i * 4 + 1] << 16)
             | (b[off + i * 4 + 2] << 8) | b[off + i * 4 + 3];
      }
      for (let i = 16; i < 64; i++) {
        const s0 = rotr(w[i - 15], 7) ^ rotr(w[i - 15], 18) ^ (w[i - 15] >>> 3);
        const s1 = rotr(w[i - 2], 17) ^ rotr(w[i - 2], 19) ^ (w[i - 2] >>> 10);
        w[i] = (w[i - 16] + s0 + w[i - 7] + s1) | 0;
      }
      let [a, b1, c, d, e, f, g, hh] = h;
      for (let i = 0; i < 64; i++) {
        const S1 = rotr(e, 6) ^ rotr(e, 11) ^ rotr(e, 25);
        const ch = (e & f) ^ (~e & g);
        const t1 = (hh + S1 + ch + K[i] + w[i]) | 0;
        const S0 = rotr(a, 2) ^ rotr(a, 13) ^ rotr(a, 22);
        const mj = (a & b1) ^ (a & c) ^ (b1 & c);
        const t2 = (S0 + mj) | 0;
        hh = g; g = f; f = e; e = (d + t1) | 0;
        d = c; c = b1; b1 = a; a = (t1 + t2) | 0;
      }
      // Updated in place. Allocating a fresh state array per 64-byte block
      // meant a million allocations per 64 MB, and hashing a timetable took
      // longer than downloading it.
      h[0] = (h[0] + a) | 0; h[1] = (h[1] + b1) | 0;
      h[2] = (h[2] + c) | 0; h[3] = (h[3] + d) | 0;
      h[4] = (h[4] + e) | 0; h[5] = (h[5] + f) | 0;
      h[6] = (h[6] + g) | 0; h[7] = (h[7] + hh) | 0;
    }

    return {
      update(chunk) {
        total += chunk.length;
        let i = 0;
        if (bufLen) {
          const need = Math.min(64 - bufLen, chunk.length);
          buf.set(chunk.subarray(0, need), bufLen);
          bufLen += need; i = need;
          if (bufLen === 64) { block(buf, 0); bufLen = 0; }
        }
        for (; i + 64 <= chunk.length; i += 64) block(chunk, i);
        if (i < chunk.length) {
          buf.set(chunk.subarray(i), 0);
          bufLen = chunk.length - i;
        }
      },
      hex() {
        const bits = total * 8;
        const pad = new Uint8Array(bufLen < 56 ? 64 : 128);
        pad.set(buf.subarray(0, bufLen), 0);
        pad[bufLen] = 0x80;
        // Length as a 64-bit big-endian count of bits. Files here are well
        // under 2^32 bytes, so the high word is only ever the top 3 bits.
        const dv = new DataView(pad.buffer);
        dv.setUint32(pad.length - 8, Math.floor(bits / 4294967296));
        dv.setUint32(pad.length - 4, bits >>> 0);
        for (let i = 0; i < pad.length; i += 64) block(pad, i);
        return Array.from(h).map((x) =>
          (x >>> 0).toString(16).padStart(8, "0")).join("");
      },
    };
  }
