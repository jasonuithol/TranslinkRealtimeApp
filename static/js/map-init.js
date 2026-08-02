// initMap: basemap, sources, layers, handlers, the mapReady hook
// Split from index.html; these files load in order as classic
// scripts and share one global scope — boot.js must stay last.
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
    // The locate button: show the rider's dot and fly the camera to it.
    // Same gate as the "near me" search button — geolocation needs a secure
    // context, so on plain http the control simply isn't offered.
    if ("geolocation" in navigator && window.isSecureContext) {
      const wrap = document.createElement("div");
      wrap.className = "maplibregl-ctrl maplibregl-ctrl-group";
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "locate-me";
      btn.title = "Show my location";
      btn.setAttribute("aria-label", "Show my location");
      btn.innerHTML =
        '<svg viewBox="0 0 24 24" width="18" height="18" aria-hidden="true">'
        + '<circle cx="12" cy="12" r="6" fill="none" stroke="currentColor" stroke-width="2"/>'
        + '<circle cx="12" cy="12" r="1.6" fill="currentColor"/>'
        + '<path d="M12 1v4M12 19v4M1 12h4M19 12h4" stroke="currentColor" stroke-width="2"/>'
        + '</svg>';
      btn.addEventListener("click", () => {
        startMeMarker(true);  // arms the live dot; the tap is iOS's compass gesture
        btn.classList.add("locating");
        navigator.geolocation.getCurrentPosition((pos) => {
          btn.classList.remove("locating");
          const ll = [pos.coords.longitude, pos.coords.latitude];
          placeMe(ll);
          // Flying to the rider is the user taking the camera — the next
          // refresh must not snap it back to the stop and its vehicles.
          // (movestart only clears autoFit on moves carrying a DOM event,
          // and this one is programmatic.)
          autoFit = false;
          cameraMove(() => map.flyTo({ center: ll,
                                       zoom: Math.max(map.getZoom(), 16) }));
        }, () => {
          btn.classList.remove("locating");
          btn.classList.add("locate-fail");
          btn.title = "Could not get your location";
          setTimeout(() => { btn.classList.remove("locate-fail");
                             btn.title = "Show my location"; }, 2500);
        }, { enableHighAccuracy: true, timeout: 10000, maximumAge: 5000 });
      });
      wrap.appendChild(btn);
      map.addControl({ onAdd: () => wrap, onRemove: () => wrap.remove() },
                     "top-right");
    }
    // The destination pin: drag it off the button and onto the map, and the
    // journey planner starts to wherever it lands. Availability mirrors the
    // "Plan a trip" button — there must be an origin (a stop or a pinned
    // address) to plan FROM — so syncChrome shows/hides it via pinCtl.
    {
      const PIN_SVG =
        '<svg viewBox="0 0 24 24" width="18" height="18" aria-hidden="true">'
        + '<path d="M12 2C8.1 2 5 5.1 5 9c0 5 7 13 7 13s7-8 7-13'
        + 'c0-3.9-3.1-7-7-7z" fill="currentColor"/>'
        + '<circle cx="12" cy="9" r="2.6" fill="#fff"/></svg>';
      const wrap = document.createElement("div");
      wrap.className = "maplibregl-ctrl maplibregl-ctrl-group";
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "drop-pin";
      btn.title = "Drag onto the map to plan a trip there";
      btn.setAttribute("aria-label", "Drag onto the map to set a destination");
      btn.innerHTML = PIN_SVG;
      // Pointer events cover mouse AND touch with one code path; capturing
      // on the button keeps move/up firing wherever the finger goes
      // (touch-action: none in the CSS stops the page scrolling instead).
      let ghost = null, downAt = null;
      const moveGhost = (e) => {
        if (!ghost) return;
        ghost.style.left = `${e.clientX}px`;
        ghost.style.top = `${e.clientY}px`;
      };
      const dropGhost = () => { if (ghost) { ghost.remove(); ghost = null; } };
      btn.addEventListener("contextmenu", (e) => e.preventDefault());
      btn.addEventListener("pointerdown", (e) => {
        e.preventDefault();
        btn.setPointerCapture(e.pointerId);
        downAt = [e.clientX, e.clientY];
        ghost = document.createElement("div");
        ghost.className = "pin-ghost";
        ghost.innerHTML = PIN_SVG;
        document.body.appendChild(ghost);
        moveGhost(e);
      });
      btn.addEventListener("pointermove", moveGhost);
      btn.addEventListener("pointercancel", dropGhost);
      btn.addEventListener("pointerup", (e) => {
        if (!ghost) return;
        dropGhost();
        // A tap is not a drop: the button sits ON the map, so without a
        // real drag the pin would land right under the button. And only a
        // drop actually on the map places the destination — anywhere else
        // is a change of heart, and costs nothing.
        if (Math.hypot(e.clientX - downAt[0], e.clientY - downAt[1]) < 8) return;
        const rect = $("map").getBoundingClientRect();
        const x = e.clientX - rect.left, y = e.clientY - rect.top;
        if (x < 0 || y < 0 || x > rect.width || y > rect.height) return;
        const ll = map.unproject([x, y]);
        setDestPoint(ll.lat, ll.lng, "dropped pin");
      });
      wrap.appendChild(btn);
      map.addControl({ onAdd: () => wrap, onRemove: () => wrap.remove() },
                     "top-left");
      pinCtl = wrap;
      syncChrome();   // the control just appeared: hide it if there's no origin
    }
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
      flushMeMarker();      // a GPS fix that beat the map to it
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

