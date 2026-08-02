// shape / trip-stop fetchers and the stop landmark layers
// Split from index.html; these files load in order as classic
// scripts and share one global scope — boot.js must stay last.
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
      if (planData) { renderPlan(); drawPlan(); }  // a planner leg waited on this
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

