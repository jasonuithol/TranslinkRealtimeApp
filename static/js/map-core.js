// map shared state, colour pools, mode glyphs, colour assignment
// Split from index.html; these files load in order as classic
// scripts and share one global scope — boot.js must stay last.
  // --- map ---------------------------------------------------------------
  // The basemap is a self-hosted Protomaps extract on the data volume. If the
  // deployment has no basemap, /api/config says so and the map stays hidden —
  // the board is the product, the map is an enhancement.
  let map = null, mapReady = false, hasBasemap = false;
  // The view auto-fits to the stop plus every tracked vehicle. It stops doing
  // so the moment the user moves the map themselves, and resumes on the next
  // stop selection. `programmatic` distinguishes our own camera moves from
  // theirs — including the +/- buttons, which move the map without a DOM event.
  let autoFit = true, forceFit = true, programmatic = false;

  // Twelve hues at 30° spacing. Every departure gets one now, not just the
  // tracked ones, so the palette has to cover a full board. Colour identifies
  // a *service*; it must not shift when that service gains or loses a live
  // position, which is why live/scheduled is shown separately.
  const VEHICLE_COLORS = [
    "#ff5a52", // red
    "#ff8a2b", // orange
    "#ffc21f", // amber
    "#d8e02a", // yellow-lime
    "#7ee02a", // lime
    "#2fd07a", // green
    "#25d3b0", // teal
    "#22d3ee", // cyan
    "#4e8cff", // blue
    "#8a7bff", // indigo
    "#c06bff", // purple
    "#ff5fb0", // magenta
  ];
  // Landmarks: the stops themselves, never a service.
  const LANDMARK_INK = "#8c98a4";          // grey — every landmark
  const LANDMARK_SELECTED_INK = "#ffffff"; // white — the stop being viewed

  // Mode glyphs, rendered in the monochrome Noto Emoji face so they tint with
  // `color` like ordinary text. GTFS route_type: 0 tram, 1 metro, 2 rail,
  // 3 bus, 4 ferry.
  const MODE_EMOJI = {
    0: "\u{1F68A}", // 🚊 tram
    1: "\u{1F686}", // 🚆 train
    2: "\u{1F686}", // 🚆 train
    3: "\u{1F68D}", // 🚍 bus
    4: "\u{26F4}",  // ⛴ ferry
  };
  const DEFAULT_EMOJI = MODE_EMOJI[3];

  // Landmark glyphs — the physical stop, not the service calling at it.
  const STOP_STATION = "\u{1F3EB}";  // 🏫
  const STOP_BUS = "\u{1F68F}";      // 🚏
  const STOP_TRAM = "\u{1F689}";     // 🚉
  const STOP_FERRY = "\u{2638}";     // ☸ wharf — the vehicle keeps ⛴

  // Where the arrival time came from.
  const MARK_LIVE = "\u{1F6DC}";      // 🛜 realtime prediction
  const MARK_SCHEDULED = "\u{1F4C5}"; // 📅 timetable only
  const MARK_ALERT = "\u{26A0}";      // ⚠ service disruption

  // These codepoints default to *emoji presentation*, and browsers hand those
  // to the system colour-emoji font regardless of font-family — so on a machine
  // with Noto Color Emoji installed our monochrome face is ignored. U+FE0E
  // (VARIATION SELECTOR-15) requests text presentation instead, which lets the
  // font-family stack apply. Only for the DOM: canvas shapes text itself and
  // already picks the right face.
  const asText = (glyph) => glyph + "︎";

  function landmarkGlyph(routeType) {
    switch (Number(routeType)) {
      case 3: return STOP_BUS;
      case 4: return STOP_FERRY;
      case 0: return STOP_TRAM;
      case 1: case 2: return STOP_STATION;
      default: return STOP_BUS;
    }
  }

  // The stop being viewed, judged by what actually calls there rather than by
  // location_type, which cannot tell a ferry terminal from a bus stop.
  function stopGlyph(data) {
    // If trains call here it is a train station, even when buses out-number
    // them — Varsity Lakes is a station with 10 bus bays, not a bus stop. The
    // majority vote below is only for deciding between the non-rail modes.
    if (data.departures.some((d) => [1, 2].includes(Number(d.route_type)))) {
      return STOP_STATION;
    }
    const counts = {};
    data.departures.forEach((d) => {
      counts[d.route_type] = (counts[d.route_type] || 0) + 1;
    });
    const top = Object.entries(counts).sort((a, b) => b[1] - a[1])[0];
    if (top) return landmarkGlyph(Number(top[0]));
    return data.stop.location_type === 1 ? STOP_STATION : STOP_BUS;
  }

  // A service keeps its colour. Once assigned, a trip_id holds that colour for
  // as long as it is remembered — across refreshes, as the board advances, and
  // whether or not it currently has a live position. Insertion-ordered, so the
  // oldest entry is the first evicted.
  const colorMemory = new Map();   // trip_id -> palette index
  const COLOR_MEMORY_MAX = 300;    // a few hours of a busy board

  // Of the colours not already on screen, take the one furthest around the
  // wheel from those that are — so a newly arriving service is as distinct as
  // the remaining palette allows, rather than just the next index along.
  function pickFreeIndex(usedIdx) {
    const P = VEHICLE_COLORS.length;
    const free = [];
    for (let i = 0; i < P; i++) if (!usedIdx.has(i)) free.push(i);
    if (!free.length) return usedIdx.size % P;   // more services than colours
    if (!usedIdx.size) return 0;
    let best = free[0], bestGap = -1;
    for (const i of free) {
      let gap = P;
      for (const j of usedIdx) {
        const d = Math.abs(i - j);
        gap = Math.min(gap, Math.min(d, P - d));
      }
      if (gap > bestGap) { bestGap = gap; best = i; }
    }
    return best;
  }

  function assignColors(items) {
    const out = {}, usedIdx = new Set();

    // Remembered colours win, provided nothing else on screen has taken it.
    for (const d of items) {
      const idx = colorMemory.get(d.trip_id);
      if (idx !== undefined && !usedIdx.has(idx)) {
        out[d.trip_id] = VEHICLE_COLORS[idx];
        usedIdx.add(idx);
      }
    }
    // Anything new — or displaced by a collision — takes a free colour.
    for (const d of items) {
      if (out[d.trip_id]) continue;
      const idx = pickFreeIndex(usedIdx);
      out[d.trip_id] = VEHICLE_COLORS[idx];
      usedIdx.add(idx);
      colorMemory.set(d.trip_id, idx);
    }
    // Re-insert the ones still on screen so eviction drops genuinely stale
    // trips first, not the ones currently being looked at.
    for (const d of items) {
      const idx = colorMemory.get(d.trip_id);
      colorMemory.delete(d.trip_id);
      colorMemory.set(d.trip_id, idx);
    }
    while (colorMemory.size > COLOR_MEMORY_MAX) {
      colorMemory.delete(colorMemory.keys().next().value);
    }
    return out;
  }

