// the map: basemap, vehicles, shapes, stop layers
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

  async function initMap() {
    let cfg;
    try {
      cfg = await (await fetch(api("/config"))).json();
      hasBasemap = !!cfg.basemap;
    } catch { hasBasemap = false; }
    if (!hasBasemap) return;

    // Must complete before any icon is rasterised, or the wrong glyph gets
    // baked into the cached image. ensureVehicleIcon only runs after the map's
    // 'load', which is well after this await.
    await loadEmojiFont();

    // Reveal before constructing the Map: MapLibre measures its container at
    // construction, and a display:none container gives it zero size — the map
    // then never fires 'load' and paints nothing.
    $("map-wrap").hidden = false;
    // Only go two-column once there is actually a map to put in the second one.
    $("split").classList.add("side-by-side");
    document.querySelector("main").classList.add("wide");
    // Browsing (arrived via a cross-region pan) or a pinned browse
    // (shared planner URLs carry pin/to but no ?at=): open the map fully.
    if (browsing || pinnedBrowse) {
      $("map-empty").hidden = true;
      $("map-caption").hidden = false;
      $("map-wrap").classList.remove("awaiting");
    }

    // {metadata: true} is required: the style's source uses `url:`, so MapLibre
    // asks the protocol for TileJSON. Without this flag pmtiles ignores that
    // request — the promise never settles, and the style hangs with no error.
    maplibregl.addProtocol("pmtiles", new pmtiles.Protocol({ metadata: true }).tile);
    // The style is one hand-written file; only the basemap archive differs per
    // region, so fetch the style and point its source at this region's pmtiles.
    // ?v bumped whenever the style schema changes. Browsers heuristically
    // cached this JSON before it was served no-cache, and a stale style from
    // the old basemap schema fails layer-by-layer against the new tiles —
    // water happened to render (both schemas have a "water" layer), roads and
    // labels silently didn't. A new URL is a guaranteed cache miss.
    const style = await (await fetch("/static/map-style.json?v=3")).json();
    style.sources.omt.url = "pmtiles://" + cfg.basemap_url;
    map = new maplibregl.Map({
      container: "map",
      style,
      // The handed-over camera when arriving from a pan swap; otherwise the
      // region's city until a stop is chosen.
      center: atParam ? [atParam.lon, atParam.lat]
            : pinParam ? [pinParam.lon, pinParam.lat] : cfg.center,
      zoom: atParam ? atParam.zoom : pinParam ? 15 : 11,
      attributionControl: { compact: true },
    });
    window.__map = map;   // debug/UI-test handle (see also ?mapdebug=1)
    map.addControl(new maplibregl.NavigationControl({ showCompass: false }), "top-right");
    map.addControl(new maplibregl.ScaleControl({ maxWidth: 120 }), "bottom-left");
    // A bad style silently yields a blank map: MapLibre reports it here and
    // nowhere else, and a style error stops 'load' firing at all, so every
    // layer we add in that handler never exists. Surface it rather than let
    // the map look merely empty.
    map.on("error", (e) => {
      const msg = (e && e.error && e.error.message) || "map error";
      console.error("[map]", msg);
      const el = $("map-status");
      if (el) el.textContent = `map: ${msg}`;
    });

    // Sanity radius per region: pan the map from one city into another and
    // the providers swap underneath — once the centre comes within
    // REGION_RADIUS_KM of another region's home city, hand the camera over.
    // Every moveend is checked: our own programmatic moves (fit, resize,
    // ping) never leave the region, so only a real cross-country pan can
    // trigger it. The zoom guard keeps a zoomed-out whole-country view from
    // swapping under the cursor before the user has picked a city.
    const REGION_RADIUS_KM = 150;
    const haversineKm = (la1, lo1, la2, lo2) => {
      const rad = Math.PI / 180;
      const a = Math.sin((la2 - la1) * rad / 2) ** 2
              + Math.cos(la1 * rad) * Math.cos(la2 * rad)
              * Math.sin((lo2 - lo1) * rad / 2) ** 2;
      return 2 * 6371 * Math.asin(Math.sqrt(a));
    };
    // Whether the move in flight is the user's own (drag / wheel / pinch).
    // Programmatic moves never carry a DOM event; each gesture resets it.
    let userDrove = false;
    map.on("movestart", (e) => { userDrove = Boolean(e.originalEvent); });
    map.on("moveend", () => {
      // With a stop on the board, only the user's own pan can hand over, and
      // only once they've left the stop's area far behind. Regions overlap —
      // several qld towns (Stradbroke, Kilcoy, Maleny, Toowoomba, …) sit
      // within 150 km of Brisbane, so a qld board's own fit-to-stop used to
      // hand the camera to seq and dump the stop the user had just picked;
      // an unconditional `if (stopId) return` fixed that but also killed
      // panning from one city into another whenever a board was open — the
      // map just ran off the region's basemap into blank void.
      if (map.getZoom() < 7) return;
      const c = map.getCenter();
      if (stopId) {
        if (!userDrove) return;
        const s = lastData?.stop;
        if (!s || s.stop_lat == null) return;
        if (haversineKm(c.lat, c.lng, s.stop_lat, s.stop_lon) <= REGION_RADIUS_KM)
          return;   // still browsing around the viewed stop: never steal it
      } else if (pinParam
          && haversineKm(c.lat, c.lng, pinParam.lat, pinParam.lon) <= REGION_RADIUS_KM) {
        // Browsing around a dropped address pin: its region was chosen by
        // nearest-stop (a Toowoomba pin is qld even though Brisbane's centre
        // is nearer) — don't second-guess it until the user leaves the area.
        return;
      }
      let best = null, bestD = Infinity;
      for (const r of regionList) {
        if (!r.center) continue;
        const d = haversineKm(c.lat, c.lng, r.center[1], r.center[0]);
        if (d < bestD) { bestD = d; best = r; }
      }
      if (best && best.id !== region && bestD <= REGION_RADIUS_KM) {
        switchRegion(best.id,
          `${c.lng.toFixed(5)},${c.lat.toFixed(5)},${map.getZoom().toFixed(2)}`);
      }
    });

    map.on("load", () => {
      if (pinParam) {
        // The searched address — amber, like the goose.
        new maplibregl.Marker({ color: "#f0b429" })
          .setLngLat([pinParam.lon, pinParam.lat]).addTo(map);
      }
      map.addSource("vehicles", { type: "geojson", data: emptyFC() });
      map.addSource("ghosts", { type: "geojson", data: emptyFC() });
      map.addSource("stop", { type: "geojson", data: emptyFC() });
      map.addSource("landmarks", { type: "geojson", data: emptyFC() });
      map.addSource("all-stops", { type: "geojson", data: emptyFC() });
      map.addSource("routes", { type: "geojson", data: emptyFC() });
      map.addSource("walklines", { type: "geojson", data: emptyFC() });

      // The path each tracked vehicle is following, in that vehicle's colour.
      // Bottom of the stack: it is context for the markers, not the subject.
      map.addLayer({
        id: "routes", type: "line", source: "routes",
        layout: { "line-join": "round", "line-cap": "round" },
        paint: {
          "line-color": ["get", "color"],
          "line-width": ["interpolate", ["linear"], ["zoom"], 10, 1.5, 15, 4],
          "line-opacity": 0.55,
        },
      });
      // Planner walk legs: dashed grey, above the ride traces. Geometry is
      // the local walking graph's route where available, else a straight line.
      map.addLayer({
        id: "walklines", type: "line", source: "walklines",
        layout: { "line-join": "round", "line-cap": "round" },
        paint: {
          // Each walk carries its own pool colour (the 🚶 "vehicle"),
          // grey only when no colour was assigned.
          "line-color": ["coalesce", ["get", "color"], "#8c98a4"],
          "line-width": ["interpolate", ["linear"], ["zoom"], 10, 1.5, 15, 3],
          "line-dasharray": [1.2, 1.6],
          "line-opacity": 0.9,
        },
      });

      // Every stop in the network, once the user is close enough that street
      // detail is the point (all backstreets legible ≈ z15+). Lazily fetched on
      // first zoom past the threshold; overlap-thinned so a busy interchange
      // stays readable; clickable to select. Below the route landmarks so a
      // traced route's own stops win any tie.
      map.addLayer({
        id: "all-stops", type: "symbol", source: "all-stops",
        minzoom: 15,
        layout: {
          "icon-image": ["get", "icon"],
          // Stations slightly larger, and placed FIRST (lower sort key wins
          // collisions): a station sits exactly where bus stops cluster, and
          // without priority the overlap thinning can drop the 🏫 in favour of
          // an adjacent 🚏. NOTE: the zoom interpolate must be the OUTERMOST
          // expression — a feature `case` wrapped around it fails validation
          // and MapLibre silently drops the whole layer.
          "icon-size": ["interpolate", ["linear"], ["zoom"],
            15, ["case", ["==", ["get", "rt"], 2], 0.7, 0.55],
            17, ["case", ["==", ["get", "rt"], 2], 1.1, 0.9]],
          "symbol-sort-key": ["case", ["==", ["get", "rt"], 2], 0, 1],
          // Near-coincident stops carry a computed offset so they fan out into
          // a row instead of colliding (see maybeLoadAllStops).
          "icon-offset": ["coalesce", ["get", "off"], ["literal", [0, 0]]],
          "icon-anchor": "bottom",
          "icon-allow-overlap": false,
          "icon-padding": 2,
        },
      });

      // The selected route's stops (and the viewed stop's across-the-road
      // pairs), in grey. Drawn above all-stops so a traced route's own stops
      // win any tie. Stations get no special layer — they are stops like any
      // other, in all-stops past zoom 15 and here when on a traced route.
      map.addLayer({
        id: "landmarks", type: "symbol", source: "landmarks",
        layout: {
          "icon-image": ["get", "icon"],
          "icon-size": ["interpolate", ["linear"], ["zoom"], 11, 0.6, 15, 1.05],
          "icon-anchor": "bottom",
          // Unlike the vehicles, these may crowd: let MapLibre thin them out.
          "icon-allow-overlap": false,
          "icon-padding": 2,
        },
      });

      // Ghosts: timetable-estimated positions for board trips with no live
      // GPS. Drawn with exactly the same weight as the live dots — the map
      // no longer distinguishes scheduled from live (the halo-ring-for-
      // estimated idea was dubious, and the ring is now the off-screen edge
      // marker's shape); the board's 🛜/📅 column carries that fact.
      map.addLayer({
        id: "ghost-ping", type: "circle", source: "ghosts",
        filter: PING_OFF,
        paint: {
          "circle-radius": 10,
          "circle-color": ["get", "color"],
          "circle-opacity": 0.35,
          "circle-stroke-width": 2,
          "circle-stroke-color": ["get", "color"],
          "circle-stroke-opacity": 0.9,
        },
      });
      map.addLayer({
        id: "ghost-dot", type: "symbol", source: "ghosts",
        layout: {
          "icon-image": ["get", "icon"],
          "icon-size": ["interpolate", ["linear"], ["zoom"], 10, 0.9, 15, 1.5],
          "icon-allow-overlap": true,
          // Always drawn, but must not BLOCK the street furniture: a marker
          // sitting at a stop would otherwise suppress the nearby stop icons
          // on the overlap-thinned layers (a station right beside the viewed
          // stop was losing that collision and vanishing).
          "icon-ignore-placement": true,
        },
      });
      map.addLayer({
        id: "ghost-label", type: "symbol", source: "ghosts",
        layout: {
          // "~" = estimated: unambiguous, and cheaper than any ring was.
          "text-field": ["concat", "~", ["get", "minutes"], " min"],
          "text-font": ["Noto Sans Medium"],
          "text-size": 11, "text-offset": [0, 2.3],
          "text-allow-overlap": false,
        },
        paint: {
          "text-color": ["coalesce", ["get", "color"], "#c9d4de"],
          "text-halo-color": "#0c1013", "text-halo-width": 1.4,
        },
      });

      map.addLayer({
        id: "stop-ring", type: "symbol", source: "stop",
        layout: {
          "icon-image": ["get", "icon"],
          "icon-size": ["interpolate", ["linear"], ["zoom"], 10, 0.975, 15, 1.575],
          "icon-allow-overlap": true,
          "icon-ignore-placement": true,   // see ghost-dot: never blocks stops
          "icon-anchor": "bottom",   // the marker sits on the stop, not over it
        },
      });
      // Expanding ring used to ping one vehicle. Below the icons so it never
      // obscures them; filtered to nothing until a ping is requested.
      map.addLayer({
        id: "vehicle-ping", type: "circle", source: "vehicles",
        filter: PING_OFF,
        paint: {
          "circle-radius": 10,
          "circle-color": ["get", "color"],
          "circle-opacity": 0.35,
          "circle-stroke-width": 2,
          "circle-stroke-color": ["get", "color"],
          "circle-stroke-opacity": 0.9,
        },
      });

      map.addLayer({
        id: "vehicle-dot", type: "symbol", source: "vehicles",
        layout: {
          // The mode emoji, tinted to match the board row's badge and stripe.
          "icon-image": ["get", "icon"],
          "icon-size": ["interpolate", ["linear"], ["zoom"], 10, 0.9, 15, 1.5],
          "icon-allow-overlap": true,
          "icon-ignore-placement": true,   // see ghost-dot: never blocks stops
        },
      });
      map.addLayer({
        id: "vehicle-label", type: "symbol", source: "vehicles",
        layout: {
          // Minutes-away is what ties a dot to its row at a glance; the route
          // number is often identical across several vehicles here.
          "text-field": ["concat", ["get", "minutes"], " min"],
          "text-font": ["Noto Sans Medium"],
          "text-size": 11,
          "text-offset": [0, 2.3],
          "text-allow-overlap": false,
        },
        paint: {
          "text-color": ["coalesce", ["get", "color"], "#c9d4de"],
          "text-halo-color": "#0c1013", "text-halo-width": 1.4,
        },
      });

      map.on("click", "vehicle-dot", (e) => {
        const p = e.features[0].properties;
        new maplibregl.Popup({ closeButton: false, offset: 12 })
          .setLngLat(e.features[0].geometry.coordinates)
          .setHTML(
            `<div class="pop-route">${p.route ?? ""} · ${p.headsign ?? ""}</div>` +
            `<div class="pop-sub">${p.minutes} min away${p.age ? " · seen " + p.age + "s ago" : ""}</div>`)
          .addTo(map);
        flashRow(p.trip);
        selectRoute(p.trip);
      });

      // Same as a vehicle click, but the popup owns up to being an estimate.
      map.on("click", "ghost-dot", (e) => {
        const p = e.features[0].properties;
        new maplibregl.Popup({ closeButton: false, offset: 12 })
          .setLngLat(e.features[0].geometry.coordinates)
          .setHTML(
            `<div class="pop-route">${p.route ?? ""} · ${p.headsign ?? ""}</div>` +
            `<div class="pop-sub">~${p.minutes} min · estimated from timetable (no live GPS)</div>`)
          .addTo(map);
        flashRow(p.trip);
        selectRoute(p.trip);
      });

      // One handler decides what a map click does, so overlapping markers
      // resolve deterministically. Two per-layer handlers used to both fire
      // when a bus stop and an always-on station overlapped, and the station's
      // won — so the bus stop "did nothing". Priority now: a vehicle/ghost keeps
      // its own popup handler; else a route bus stop beats a background station
      // (you are clicking the stop you traced); else empty map clears the route.
      const stopLayers = ["landmarks", "all-stops"];
      map.on("click", (e) => {
        // Vehicles and ghosts have their own handlers below; leave those to them.
        if (map.queryRenderedFeatures(e.point, { layers: ["vehicle-dot", "ghost-dot"] }).length) return;
        const stops = map.queryRenderedFeatures(e.point, { layers: stopLayers });
        if (stops.length) {
          const pick = stops.find((f) => f.layer.id === "landmarks") || stops[0];
          if (pick.properties.stop_id)
            selectStop(pick.properties.stop_id, pick.properties.region);
          return;
        }
        if (selectedTrip) selectRoute(selectedTrip);   // empty map → clear route
      });
      for (const lyr of ["vehicle-dot", "ghost-dot", ...stopLayers]) {
        map.on("mouseenter", lyr, () => map.getCanvas().style.cursor = "pointer");
        map.on("mouseleave", lyr, () => map.getCanvas().style.cursor = "");
      }

      // Hovering a stop names it: one shared tooltip that follows the
      // cursor from stop to stop and vanishes on leave. setText keeps the
      // feed-supplied name inert. (No hover on touch devices — tapping
      // already opens the stop's board.)
      const stopTip = new maplibregl.Popup({
        closeButton: false, closeOnClick: false, offset: 12,
      });
      for (const lyr of stopLayers) {
        map.on("mousemove", lyr, (e) => {
          const f = e.features && e.features[0];
          const name = f && (f.properties.name || f.properties.stop_name);
          if (!name) return;
          stopTip.setLngLat(f.geometry.coordinates).setText(name).addTo(map);
        });
        map.on("mouseleave", lyr, () => stopTip.remove());
      }

      // Only a move carrying a DOM event is the user taking control — a drag,
      // a wheel, a pinch. Testing `programmatic` instead looked equivalent but
      // was not: map.resize() below fires movestart with the flag clear, which
      // switched auto-fit off during startup and left it off forever. The cost
      // is that the +/- buttons no longer disable auto-fit, which is the far
      // less annoying failure.
      map.on("movestart", (e) => { if (e.originalEvent) autoFit = false; });
      map.on("moveend", () => { programmatic = false; });
      // Edge markers track the viewport continuously — a pan or zoom changes
      // which vehicles are off-screen and where their arrows point.
      map.on("move", syncEdgeMarkers);
      map.on("resize", syncEdgeMarkers);

      mapReady = true;
      cameraMove(() => map.resize());
      map.on("zoomend", maybeLoadAllStops);
      maybeLoadAllStops();  // in case the map starts past the threshold
      loadPinSurrounds();   // pin present: draw + fit its surrounding stops
      if (planData) drawPlan();   // a plan booted before the map was ready
      if (lastData) updateMap(lastData);
    });

    // ?mapdebug=1 — ground-truth probe, run in the viewer's own browser (the
    // dev harness cannot render MapLibre at all). On every idle it logs which
    // style is actually loaded and how many features each basemap layer really
    // rendered. Console, not the caption — the 15s poll overwrites the caption.
    if (new URLSearchParams(location.search).get("mapdebug")) {
      map.on("idle", () => {
        const s = map.getStyle();
        const counts = {};
        for (const id of ["water", "road-major", "road-secondary", "road-minor",
                          "busway", "rail", "place", "landcover-green",
                          "all-stops", "landmarks", "vehicle-dot", "ghost-dot"]) {
          try {
            counts[id] = map.getLayer(id)
              ? map.queryRenderedFeatures({ layers: [id] }).length
              : "NO-LAYER";
          } catch (e) { counts[id] = "ERR:" + e.message; }
        }
        console.log(`[mapdebug] style=${s.name} zoom=${map.getZoom().toFixed(1)}`, counts);
      });
    }
  }

  const emptyFC = () => ({ type: "FeatureCollection", features: [] });

  // A merged board can carry rows borrowed from a sibling region (the XPT on
  // Central's suburban board) — every per-trip fetch must go to the region
  // the row came from, not the board's.
  const tripRegion = (tripId) =>
    (lastData?.departures || []).find((d) => d.trip_id === tripId)?.region || region;
  const apiIn = (rgn, path) => `/api/r/${rgn || region}${path}`;

  // Route geometry is static for the life of a timetable, so fetch each shape
  // once and keep it. `pending` stops a slow fetch being issued repeatedly by
  // successive polls.
  const shapeCache = new Map(), shapePending = new Set();

  async function ensureShape(shapeId, rgn) {
    if (!shapeId || shapeCache.has(shapeId) || shapePending.has(shapeId)) return;
    shapePending.add(shapeId);
    try {
      const res = await fetch(apiIn(rgn, `/shape/${encodeURIComponent(shapeId)}`));
      if (res.ok) {
        shapeCache.set(shapeId, (await res.json()).points);
        if (lastData) drawRoutes(lastData);   // redraw once it lands
        if (planData) drawPlan();             // planner legs wait on shapes too
      } else {
        shapeCache.set(shapeId, null);        // no geometry; stop asking
      }
    } catch {
      /* leave it unset so a later poll can retry */
    } finally {
      shapePending.delete(shapeId);
    }
  }

  // Nothing is drawn until a service is picked. Showing every route at once
  // stacked identical paths on top of each other and told you nothing; one
  // route, on demand, is what is actually useful. The same goes for the stops
  // along it — 191 grey pins for a station board was clutter, not context.
  let selectedTrip = null;

  const tripStopsCache = new Map(), tripStopsPending = new Set();

  async function ensureTripStops(tripId) {
    if (!tripId || tripStopsCache.has(tripId) || tripStopsPending.has(tripId)) return;
    tripStopsPending.add(tripId);
    try {
      // ?v bumped on schema change (v2: scheduled times added) — the
      // response is cached for a day, same lesson as all-stops.
      const res = await fetch(
        apiIn(tripRegion(tripId), `/trip-stops/${encodeURIComponent(tripId)}?v=2`));
      tripStopsCache.set(tripId, res.ok ? (await res.json()).stops : null);
      drawLandmarks();
      renderTripPanel();
      if (planData) drawPlan();   // a planner leg was waiting on this trip
    } catch {
      /* leave unset so a later selection retries */
    } finally {
      tripStopsPending.delete(tripId);
    }
  }

  // Grey landmarks: the selected route's stops (when one is traced) plus the
  // paired stops across the road from the viewed stop (always — "going the
  // other way" is the next thing a rider looks for). Never the viewed stop
  // itself; that is drawn separately in white.
  function drawLandmarks() {
    if (!mapReady || !map.getSource("landmarks")) return;
    const routeStops = (selectedTrip && tripStopsCache.get(selectedTrip)) || [];
    const paired = lastData?.paired || [];
    const selfId = lastData?.stop?.stop_id;
    // A traced trip may belong to a sibling region (an XPT on Central's
    // board): its stops must open in *that* region, so each landmark
    // remembers where it came from.
    const routeRgn = selectedTrip ? tripRegion(selectedTrip) : region;
    const seen = new Set();
    const features = [];
    for (const [l, rgn] of [
      ...routeStops.map((l) => [l, routeRgn]),
      ...paired.map((l) => [l, region]),
      ...pinStops.map((l) => [l, l.region || region]),
    ]) {
      if (l.stop_id === selfId || seen.has(l.stop_id)) continue;
      seen.add(l.stop_id);
      features.push({
        type: "Feature",
        geometry: { type: "Point", coordinates: [l.lon, l.lat] },
        properties: {
          stop_id: l.stop_id,   // click a landmark to view it as the stop
          region: rgn,
          name: l.stop_name,
          icon: ensureVehicleIcon(landmarkGlyph(l.route_type), LANDMARK_INK),
        },
      });
    }
    map.getSource("landmarks").setData({ type: "FeatureCollection", features });
  }

  // Hide only the stop being viewed from the all-stops layer — it already has
  // the white viewed-stop marker, and a grey one underneath just doubles up.
  function syncRailStationFilter() {
    if (!mapReady) return;
    if (map.getLayer("all-stops")) {
      map.setFilter("all-stops", ["!=", ["get", "stop_id"], stopId || ""]);
    }
  }

  // Every stop in the network, fetched once, lazily — only when the user first
  // zooms in far enough for the all-stops layer (minzoom 15) to matter. ~1 MB
  // of GeoJSON that most sessions never need has no place in the initial load.
  let allStopsLoaded = false, allStopsLoading = false;
  function maybeLoadAllStops() {
    if (allStopsLoaded || allStopsLoading || !mapReady) return;
    if (map.getZoom() < 14.5) return;   // fetch just ahead of the threshold
    allStopsLoading = true;
    // ?v bumped when the payload schema changes — the endpoint is cached for a
    // day (rightly: 1-2.5 MB, changes weekly), so a schema change served under
    // the same URL leaves clients on the old shape until the cache expires.
    // v2: rail stations included, tagged rt=2.
    // ?v bumped whenever the payload's meaning changes (v3: parent stations
    // inherit their children's dominant mode — ferry terminals stopped
    // reading as bus stops); the response is cached for 24 h.
    fetch(api("/all-stops?v=3"))
      .then((r) => r.json())
      .then((d) => {
        const stops = d.stops || [];
        // Near-coincident stops (a station beside its bus interchange,
        // kerbside pairs) would collide and thin each other out. Cluster by
        // true proximity — union-find over a coarse cell index, linking stops
        // within ~28 m even across cell boundaries (a plain grid hash missed
        // exactly the station-next-to-stop case) — then fan each cluster out
        // into a little row so neighbours sit next to each other instead of
        // fighting for one spot. Offsets are icon pixels (scaled by icon-size).
        const CELL = 0.0004, LINK_M = 28;
        const cells = new Map();
        stops.forEach((s, i) => {
          const key = `${Math.round(s.lat / CELL)},${Math.round(s.lon / CELL)}`;
          (cells.get(key) || cells.set(key, []).get(key)).push(i);
        });
        const root = stops.map((_, i) => i);
        const find = (i) => root[i] === i ? i : (root[i] = find(root[i]));
        const metres = (a, b) => {
          const kx = 111320 * Math.cos(a.lat * Math.PI / 180);
          return Math.hypot((a.lon - b.lon) * kx, (a.lat - b.lat) * 110540);
        };
        stops.forEach((s, i) => {
          const cy = Math.round(s.lat / CELL), cx = Math.round(s.lon / CELL);
          for (let dy = -1; dy <= 1; dy++) for (let dx = -1; dx <= 1; dx++) {
            for (const j of cells.get(`${cy + dy},${cx + dx}`) || []) {
              if (j > i && metres(s, stops[j]) <= LINK_M) root[find(j)] = find(i);
            }
          }
        });
        const groups = new Map();
        stops.forEach((s, i) => {
          const r = find(i);
          (groups.get(r) || groups.set(r, []).get(r)).push(s);
        });
        const offsetOf = new Map();
        for (const g of groups.values()) {
          if (g.length < 2) continue;
          g.sort((a, b) => (a.route_type ?? 9) - (b.route_type ?? 9)
                        || String(a.stop_id).localeCompare(String(b.stop_id)));
          g.forEach((s, i) => {
            // rows of three, centred
            const row = Math.floor(i / 3), col = i % 3, n = Math.min(g.length - row * 3, 3);
            offsetOf.set(s.stop_id, [(col - (n - 1) / 2) * 30, row * 30]);
          });
        }
        map.getSource("all-stops").setData({
          type: "FeatureCollection",
          features: stops.map((s) => ({
            type: "Feature",
            geometry: { type: "Point", coordinates: [s.lon, s.lat] },
            properties: {
              stop_id: s.stop_id,
              name: s.name,
              rt: Number(s.route_type),
              off: offsetOf.get(s.stop_id) || [0, 0],
              icon: ensureVehicleIcon(landmarkGlyph(s.route_type), LANDMARK_INK),
            },
          })),
        });
        allStopsLoaded = true;
        syncRailStationFilter();
      })
      .catch(() => { allStopsLoading = false; });   // retry on a later zoom
  }

  function drawRoutes(data) {
    if (!mapReady || !map.getSource("routes")) return;
    if (!selectedTrip) {
      map.getSource("routes").setData(emptyFC());
      return;
    }
    const row = (data.departures || []).find((d) => d.trip_id === selectedTrip);
    const pts = row && shapeCache.get(row.shape_id);
    if (!pts) {
      if (row) ensureShape(row.shape_id, row.region);
      map.getSource("routes").setData(emptyFC());
      return;
    }
    map.getSource("routes").setData({
      type: "FeatureCollection",
      features: [{
        type: "Feature",
        geometry: { type: "LineString", coordinates: pts },
        properties: { color: (data.__colors || {})[selectedTrip] || "#3fb950" },
      }],
    });
  }

  // Clicking the same service again clears it.
  function selectRoute(tripId) {
    selectedTrip = selectedTrip === tripId ? null : tripId;
    if (selectedTrip) {
      // Start fetching now rather than waiting on the map's draw pass, so both
      // are usually cached by the time they are wanted.
      const d = (lastData?.departures || []).find((x) => x.trip_id === selectedTrip);
      if (d) ensureShape(d.shape_id, d.region);
      ensureTripStops(selectedTrip);
      refreshTripTimes(selectedTrip);
    }
    document.querySelectorAll(".row.selected")
      .forEach((r) => r.classList.remove("selected"));
    if (selectedTrip) {
      const el = document.querySelector(`.row[data-trip="${CSS.escape(selectedTrip)}"]`);
      if (el) el.classList.add("selected");
    }
    if (lastData) drawRoutes(lastData);
    drawLandmarks();
    renderTripPanel();
  }

  // Live per-stop arrivals for the selected trip; refetched on the board's
  // poll cadence while a trip is selected. Absence just means the panel
  // shows the timetable alone.
  const tripTimesCache = new Map();
  async function refreshTripTimes(tripId) {
    if (!tripId) return;
    try {
      const res = await fetch(
        apiIn(tripRegion(tripId), `/trip-times/${encodeURIComponent(tripId)}`));
      if (res.ok) {
        tripTimesCache.set(tripId, await res.json());
        renderTripPanel();
      }
    } catch { /* scheduled times still render */ }
  }

  // GTFS wall-clock "HH:MM:SS" (hours may exceed 24 after midnight) plus a
  // delay in seconds → "HH:MM" for display.
  const schedPlus = (hms, plusS) => {
    const [h, m, s] = String(hms).split(":").map(Number);
    const t = h * 3600 + m * 60 + (s || 0) + (plusS || 0);
    const hh = String(Math.floor(t / 3600) % 24).padStart(2, "0");
    return `${hh}:${String(Math.floor((t % 3600) / 60)).padStart(2, "0")}`;
  };

  // The selected service's full calling pattern, overlaid on the arrivals
  // list (the badge column stays visible). Rebuilt on every board render —
  // the board render wipes #board — so scroll position is carried across
  // rebuilds of the same trip.
  let panelTripRendered = null;
  function renderTripPanel() {
    const board = $("board");
    const old = board.querySelector(".trip-panel");
    const keepScroll = old && panelTripRendered === selectedTrip
      ? old.querySelector(".tstops").scrollTop : null;
    if (old) old.remove();
    panelTripRendered = null;
    board.style.minHeight = "";        // reclaimed whenever the panel closes
    if (!selectedTrip || !stopId) return;
    const d = (lastData?.departures || []).find((x) => x.trip_id === selectedTrip);
    if (!d) return;                    // service has left the board
    const stops = tripStopsCache.get(selectedTrip);
    const lt = tripTimesCache.get(selectedTrip) || {};
    const live = lt.stops || {};
    const delays = lt.delays || [];   // sorted (stop_sequence, delay) pairs
    // An update applies to every later stop until the next update — the same
    // propagation the board applies for feeds publishing only the next stop.
    const propDelay = (seq) => {
      let d = null;
      for (const [s, dl] of delays) { if (s <= seq) d = dl; else break; }
      return d;
    };
    const vcolor = (lastData.__colors || {})[selectedTrip] || "#3fb950";
    // 24-hour, to match the schedule-derived times beside it in the list.
    const fmtEpoch = (t) => new Date(t * 1000)
      .toLocaleTimeString("en-GB", {hour12: false, hour: "2-digit",
                                    minute: "2-digit",
                                    ...(regionTz ? {timeZone: regionTz} : {})});
    // Region wall-clock now, for greying stops already served. String
    // comparison is enough at HH:MM granularity; after-midnight rows
    // (GTFS hours ≥ 24 map past 00:00) may briefly mis-grey — harmless.
    const nowHM = new Date().toLocaleTimeString("en-GB",
      {hour12: false, hour: "2-digit", minute: "2-digit",
       ...(regionTz ? {timeZone: regionTz} : {})});
    const nowS = Date.now() / 1000;
    const hereId = d.stop_id || stopId;   // the boarding platform, not the parent
    let items, keptStops = [];
    if (!stops) {
      items = `<div class="tstop"><span class="tname">Loading stops…</span></div>`;
    } else {
      const recs = stops.map((s) => {
        const lr = live[s.stop_id];
        const delay = lr?.delay ?? propDelay(s.seq);
        let shown, isLive = false, past;
        if (lr?.t) {
          shown = fmtEpoch(lr.t);
          isLive = true;
          past = lr.t < nowS - 30;
        } else if (s.sched) {
          isLive = delay !== null && delay !== undefined;
          shown = schedPlus(s.sched, isLive ? delay : 0);
          past = shown < nowHM && Number(s.sched.split(":")[0]) < 24;
        } else {
          shown = "";
          past = false;
        }
        return { s, lr, shown, isLive, past: past && s.stop_id !== hereId };
      });
      // History is context, not the point: keep only the three most recently
      // served stops, and say how many came before rather than cutting
      // silently. (The viewed stop is never "past", so it is never dropped.)
      const pastIdx = recs.map((r, i) => (r.past ? i : -1)).filter((i) => i >= 0);
      const hideSet = new Set(pastIdx.slice(0, -3));
      const kept = recs.filter((_, i) => !hideSet.has(i));
      keptStops = kept.map((r) => r.s);
      items = (hideSet.size
          ? `<div class="tmore">&ctdot; ${hideSet.size} earlier stops</div>` : "")
        + kept.map(({ s, lr, shown, past }) => {
            const cls = ["tstop",
                         lr?.skipped && "skipped",
                         s.stop_id === hereId && "here",
                         past && "past"]
                        .filter(Boolean).join(" ");
            return `<div class="${cls}"><span class="tname">${s.stop_name}</span>` +
                   `<span class="ttime">${lr?.skipped ? "skipped" : shown}</span></div>`;
          }).join("");
    }
    const panel = document.createElement("div");
    panel.className = "trip-panel";
    panel.style.setProperty("--vcolor", vcolor);
    // Sit just below the board heading — the timeline must not cover which
    // stop it belongs to. (Height measured live: long names wrap.)
    const bh = board.querySelector(".board-head");
    if (bh) panel.style.top = `${bh.offsetTop + bh.offsetHeight}px`;
    // The header speaks the board's language: the service's badge, then the
    // same 🛜/📅 source mark its row carries (the panel covers that column).
    const routeLabel = d.route ?? "";
    const numClass = routeLabel.length > 8 ? "num xwide"
                   : routeLabel.length > 4 ? "num wide" : "num";
    panel.innerHTML = `
      <div class="tp-head">
        <div class="badge" style="color:${vcolor}">
          <span class="mode">${asText(MODE_EMOJI[d.route_type] ?? DEFAULT_EMOJI)}</span>
          <span class="${numClass}">${routeLabel}</span>
        </div>
        <span class="tp-src" title="${d.realtime ? "Live Location Feed" : "Scheduled/Estimated"}"
              >${asText(d.realtime ? MARK_LIVE : MARK_SCHEDULED)}</span>
        <span class="tp-dest">→ ${d.headsign ?? ""}</span>
        <button class="tp-close" aria-label="Close stop list">&times;</button>
      </div>
      <div class="tstops"><div class="ttrack">${items}</div></div>`;
    panel.querySelector(".tp-close").addEventListener("click", (ev) => {
      ev.stopPropagation();
      selectRoute(selectedTrip);       // toggles the selection off
    });
    // The rows underneath must not also react to clicks on the panel.
    panel.addEventListener("click", (ev) => ev.stopPropagation());
    board.appendChild(panel);
    const list = panel.querySelector(".tstops");

    // Side by side with the map, the stop list may outgrow the arrivals
    // list: stretch the board to the map's height so the timeline uses the
    // space instead of scrolling inside a short strip. Stacked (phone)
    // layout keeps the board height — growing it would just push the map
    // out of reach.
    // The class alone is not enough — it is set whenever a map exists, but
    // the two-column layout only engages at the stylesheet's breakpoint.
    const wrap = $("map-wrap");
    if ($("split").classList.contains("side-by-side")
        && window.matchMedia("(min-width: 900px)").matches && !wrap.hidden) {
      const mh = wrap.getBoundingClientRect().height;
      if (mh > board.getBoundingClientRect().height) {
        board.style.minHeight = `${mh}px`;
      }
    }

    // The selected service's vehicle, hung on the line at its spot: the
    // position is projected onto the stop-to-stop polyline, and the winning
    // segment's fraction is mapped onto the rendered rows. (One vehicle
    // only, by design — every stop shows one arrival time, so a second
    // vehicle on the timeline read as a contradiction.) No live GPS does
    // not mean no marker: fall back to the same estimated position the map
    // draws as a ghost — a trip can have live TIMES (TripUpdates) yet no
    // live POSITION (VehiclePositions); the 348s do exactly that.
    let vehs = (keptStops.length > 1 && lt.vehicles) || [];
    if (!vehs.length && keptStops.length > 1) {
      const g = (lastData.__ghosts || lastData.ghosts || [])
        .find((x) => x.trip_id === selectedTrip);
      if (g) vehs = [{ trip_id: g.trip_id, lat: g.lat, lon: g.lon }];
    }
    if (vehs.length) {
      const track = panel.querySelector(".ttrack");
      // Projection runs over the RENDERED stops (history beyond three served
      // stops is collapsed), so fractions and .tstop rows stay 1:1.
      const stops_ = keptStops;
      const rows = [...track.querySelectorAll(".tstop")];
      const glyph = MODE_EMOJI[d.route_type] ?? DEFAULT_EMOJI;
      const kx = 111320 * Math.cos(stops_[0].lat * Math.PI / 180), ky = 110540;
      const yOf = (i) => rows[i].offsetTop + rows[i].offsetHeight / 2;
      for (const v of vehs) {
        let best = null;
        for (let i = 0; i < stops_.length - 1; i++) {
          const ax = stops_[i].lon * kx,     ay = stops_[i].lat * ky;
          const bx = stops_[i + 1].lon * kx, by = stops_[i + 1].lat * ky;
          const px = v.lon * kx,            py = v.lat * ky;
          const dx = bx - ax, dy = by - ay;
          const t = Math.max(0, Math.min(1,
            ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy || 1)));
          const qx = ax + t * dx - px, qy = ay + t * dy - py;
          const d2 = qx * qx + qy * qy;
          if (!best || d2 < best.d2) best = { frac: i + t, d2 };
        }
        // A route-mate more than ~10 km off this trip's line is running a
        // pattern these stops don't describe — placing it would just lie.
        if (!best || best.d2 > 10000 * 10000) continue;
        const i = Math.min(Math.floor(best.frac), rows.length - 2);
        const el = document.createElement("div");
        el.className = "tveh" + (v.trip_id === selectedTrip ? " me" : "");
        el.style.top = `${yOf(i) + (best.frac - i) * (yOf(i + 1) - yOf(i))}px`;
        el.textContent = asText(glyph);
        track.appendChild(el);
      }
    }

    if (keepScroll !== null) {
      list.scrollTop = keepScroll;
    } else {
      const here = panel.querySelector(".tstop.here");
      if (here) list.scrollTop = here.offsetTop - list.clientHeight / 2;
    }
    panelTripRendered = selectedTrip;
  }

  // MapLibre draws label text from pre-built glyph PBFs, and ours cover latin
  // only — an emoji codepoint simply would not render. So the glyph is
  // rasterised to a canvas in the vehicle's colour and registered as a map
  // image instead. One image per (glyph, colour) pair, made on demand.
  const ICON_PX = 48, ICON_RATIO = 2;

  // Canvas is fed the font through an explicitly constructed FontFace rather
  // than the stylesheet's family. Relying on the @font-face rule means relying
  // on it having been parsed and matched by the time we rasterise — and when it
  // has not been, canvas silently falls back to the system colour-emoji font
  // and bakes a full-colour glyph into the cached image. This also sidesteps
  // the rule's unicode-range, which canvas font matching need not honour.
  const CANVAS_EMOJI = "NotoEmojiCanvas";
  let emojiFontReady = null;
  function loadEmojiFont() {
    if (!emojiFontReady) {
      emojiFontReady = (async () => {
        // ?v matches fonts.css and is bumped with every subset change. This
        // bare-URL load predates the no-cache middleware, so browsers held a
        // heuristically-cached old subset — any glyph added since (🚉) then
        // fell back to the system COLOUR emoji font on the canvas, which is
        // exactly the coloured-map-icons bug. A new URL is a guaranteed miss.
        const ff = new FontFace(CANVAS_EMOJI,
          "url('/static/fonts/NotoEmoji-mode.woff2?v=5')");
        await ff.load();
        document.fonts.add(ff);
      })().catch(() => { /* fall back to whatever canvas picks */ });
    }
    return emojiFontReady;
  }

  function ensureVehicleIcon(glyph, color) {
    const id = `veh-${glyph.codePointAt(0).toString(16)}-${color.replace("#", "")}`;
    if (map.hasImage(id)) return id;

    const c = document.createElement("canvas");
    c.width = c.height = ICON_PX;
    const ctx = c.getContext("2d");
    ctx.font = `${Math.round(ICON_PX * 0.7)}px "${CANVAS_EMOJI}", sans-serif`;
    ctx.textAlign = "center";
    ctx.textBaseline = "middle";
    // Dark outline first so the glyph stays legible over any basemap colour.
    ctx.lineJoin = "round";
    ctx.lineWidth = ICON_PX * 0.14;
    ctx.strokeStyle = "#0c1013";
    ctx.strokeText(glyph, ICON_PX / 2, ICON_PX / 2);
    ctx.fillStyle = color;
    ctx.fillText(glyph, ICON_PX / 2, ICON_PX / 2);

    map.addImage(id, ctx.getImageData(0, 0, ICON_PX, ICON_PX), {
      pixelRatio: ICON_RATIO,
    });
    return id;
  }

  // A filter that matches no feature — the ping ring's resting state.
  const PING_OFF = ["==", ["get", "trip"], " none"];
  const REDUCED_MOTION = window.matchMedia("(prefers-reduced-motion: reduce)");

  const FIT_PADDING = 46;   // keeps markers clear of the edges and the controls
  const FIT_MAX_ZOOM = 15;  // stop it diving in when everything is close together

  // --- linking the two views ---------------------------------------------
  let pingRaf = null, pingTimer = null;

  // Board row -> map. Expanding ring on that marker, panning to it first if it
  // has drifted out of view. Works for a live vehicle or an estimated ghost —
  // whichever holds the trip — pinging on that source's own ring layer.
  function pingVehicle(tripId) {
    if (!mapReady || !map.getLayer("vehicle-ping")) return;
    let target = (lastData?.__vehicles || []).find((x) => x.trip_id === tripId);
    let layer = "vehicle-ping";
    if (!target) {
      target = (lastData?.__ghosts || []).find((x) => x.trip_id === tripId);
      layer = "ghost-ping";
    }
    if (!target) return;

    if (!map.getBounds().contains([target.lon, target.lat])) {
      cameraMove(() => map.easeTo({ center: [target.lon, target.lat], duration: 600 }));
    }

    cancelAnimationFrame(pingRaf);
    clearTimeout(pingTimer);
    // Only one ping at a time, on either layer.
    map.setFilter("vehicle-ping", PING_OFF);
    map.setFilter("ghost-ping", PING_OFF);
    map.setFilter(layer, ["==", ["get", "trip"], tripId]);

    if (REDUCED_MOTION.matches) {
      // Static ring instead of a pulse, then clear.
      map.setPaintProperty(layer, "circle-radius", 20);
      map.setPaintProperty(layer, "circle-opacity", 0.3);
      map.setPaintProperty(layer, "circle-stroke-opacity", 0.9);
      pingTimer = setTimeout(() => map.setFilter(layer, PING_OFF), 1600);
      return;
    }

    const START = performance.now(), CYCLE = 850, TOTAL = 2550;
    const step = (t) => {
      const elapsed = t - START;
      if (elapsed > TOTAL) {
        map.setFilter(layer, PING_OFF);
        return;
      }
      const p = (elapsed % CYCLE) / CYCLE;      // 0..1 within each pulse
      map.setPaintProperty(layer, "circle-radius", 9 + p * 26);
      map.setPaintProperty(layer, "circle-opacity", 0.32 * (1 - p));
      map.setPaintProperty(layer, "circle-stroke-opacity", 0.95 * (1 - p));
      pingRaf = requestAnimationFrame(step);
    };
    pingRaf = requestAnimationFrame(step);
  }
