// vehicles on the map: camera moves, view fitting, marker updates,
// off-screen edge markers
// Split from index.html; these files load in order as classic
// scripts and share one global scope — boot.js must stay last.
  // Move the camera under our own flag, so the movestart handler does not read
  // it as the user taking over.
  function cameraMove(fn) {
    programmatic = true;
    fn();
  }

  // Frame the stop and every marker — live and estimated alike, since the point
  // is that the board and the map show the same set. Called on each poll, but
  // only actually moves when it needs to: re-animating a map every 15 seconds
  // while someone is reading it is worse than a slightly stale frame.
  function fitView(data, vehicles, ghosts = []) {
    if (!mapReady || (!autoFit && !forceFit)) return;

    const pts = [];
    const { stop_lat: lat, stop_lon: lon } = data.stop;
    if (lat != null && lon != null) pts.push([lon, lat]);
    vehicles.forEach((v) => pts.push([v.lon, v.lat]));
    ghosts.forEach((g) => pts.push([g.lon, g.lat]));
    if (!pts.length) return;

    if (pts.length === 1) {
      if (forceFit) {
        cameraMove(() => map.easeTo({ center: pts[0], zoom: 14, duration: 600 }));
        forceFit = false;
      }
      return;
    }

    const bounds = pts.reduce(
      (b, p) => b.extend(p),
      new maplibregl.LngLatBounds(pts[0], pts[0]),
    );

    if (!forceFit) {
      // Already framed well enough? Leave it alone. Refit when something has
      // moved out of view, or when the view has drifted much wider than needed
      // (a distant vehicle dropping off the board zooms us back in).
      const view = map.getBounds();
      const allVisible = pts.every((p) => view.contains(p));
      const target = map.cameraForBounds(bounds, {
        padding: FIT_PADDING, maxZoom: FIT_MAX_ZOOM,
      });
      const tooWide = target && target.zoom - map.getZoom() > 1.5;
      if (allVisible && !tooWide) return;
    }

    cameraMove(() => map.fitBounds(bounds, {
      padding: FIT_PADDING, maxZoom: FIT_MAX_ZOOM, duration: 700,
    }));
    forceFit = false;
  }

  function updateMap(data) {
    if (!mapReady) return;
    const lat = data.stop.stop_lat, lon = data.stop.stop_lon;

    if (lat != null && lon != null) {
      map.getSource("stop").setData({
        type: "FeatureCollection",
        features: [{
          type: "Feature",
          geometry: { type: "Point", coordinates: [lon, lat] },
          // White: this is the landmark that was searched for.
          properties: {
            icon: ensureVehicleIcon(stopGlyph(data), LANDMARK_SELECTED_INK),
          },
        }],
      });
    }

    drawLandmarks();

    const now = Math.floor(Date.now() / 1000);
    const colors = data.__colors || {};
    // __vehicles is the set trimmed to the rows actually on screen.
    const vehicles = data.__vehicles || data.vehicles || [];
    // route_type rides on the departures, not the vehicles, so look it up.
    const modeOf = {};
    data.departures.forEach((d) => { modeOf[d.trip_id] = d.route_type; });

    map.getSource("vehicles").setData({
      type: "FeatureCollection",
      features: vehicles.map((v) => {
        const color = colors[v.trip_id] || "#3fb950";
        const glyph = MODE_EMOJI[modeOf[v.trip_id]] ?? DEFAULT_EMOJI;
        return {
          type: "Feature",
          geometry: { type: "Point", coordinates: [v.lon, v.lat] },
          properties: {
            trip: v.trip_id,          // links the marker back to its board row
            route: v.route ?? "",
            headsign: v.headsign ?? "",
            minutes: v.minutes,
            color,
            icon: ensureVehicleIcon(glyph, color),
            age: v.timestamp ? now - v.timestamp : null,
          },
        };
      }),
    });

    // Ghosts: drawn identically to the live dots — scheduled-vs-live is the
    // board's 🛜/📅 column's job, not the map's.
    const ghosts = data.__ghosts || data.ghosts || [];
    map.getSource("ghosts").setData({
      type: "FeatureCollection",
      features: ghosts.map((g) => {
        const color = colors[g.trip_id] || "#3fb950";
        const glyph = MODE_EMOJI[modeOf[g.trip_id]] ?? DEFAULT_EMOJI;
        return {
          type: "Feature",
          geometry: { type: "Point", coordinates: [g.lon, g.lat] },
          properties: {
            trip: g.trip_id,
            route: g.route ?? "",
            headsign: g.headsign ?? "",
            minutes: g.minutes,
            color,
            icon: ensureVehicleIcon(glyph, color),
          },
        };
      }),
    });

    drawRoutes(data);
    fitView(data, vehicles, ghosts);
    syncEdgeMarkers();

    const shownCount = data.departures.length - (data.__hidden || 0);
    const live = vehicles.length, est = ghosts.length;
    $("map-status").textContent =
      live + est === 0 ? "no positions for these services"
      : est === 0 ? `${live} of ${shownCount} tracking live`
      : `${live} live · ${est} estimated`;
  }

  // A board vehicle outside the current view still deserves a presence:
  // its marker is clamped to the nearest map edge, with a rim arrow aimed
  // at the real position. Rebuilt wholesale on every map move — the set is
  // at most the board's row count, so this stays cheap.
  function syncEdgeMarkers() {
    const holder = $("edge-markers");
    if (!holder) return;
    if (!mapReady || !lastData || !stopId) { holder.replaceChildren(); return; }
    const w = map.getContainer().clientWidth;
    const h = map.getContainer().clientHeight;
    const M = 26;                     // keeps the whole ring inside the map
    const colors = lastData.__colors || {};
    const modeOf = {};
    lastData.departures.forEach((d) => { modeOf[d.trip_id] = d.route_type; });
    const frag = document.createDocumentFragment();
    const placed = [];                // markers already pinned, for fanning
    for (const v of [...(lastData.__vehicles || []), ...(lastData.__ghosts || [])]) {
      const pt = map.project([v.lon, v.lat]);
      if (pt.x >= 0 && pt.x <= w && pt.y >= 0 && pt.y <= h) continue;
      let x = Math.min(Math.max(pt.x, M), w - M);
      let y = Math.min(Math.max(pt.y, M), h - M);
      // A whole fleet off in one direction clamps to the same spot and
      // stacks. Slide colliders along the edge they are pinned to (wrapping
      // within it); each arrow is aimed from its FINAL spot, so a displaced
      // marker still points at its own vehicle.
      const slideY = x === M || x === w - M;   // on a vertical edge
      for (let guard = 0; guard < 50; guard++) {
        if (!placed.some((p) => Math.hypot(p[0] - x, p[1] - y) < 34)) break;
        if (slideY) {
          y += 34; if (y > h - M) y = M + (y - M) % (h - 2 * M);
        } else {
          x += 34; if (x > w - M) x = M + (x - M) % (w - 2 * M);
        }
      }
      placed.push([x, y]);
      const el = document.createElement("div");
      el.className = "edge-veh";
      el.style.left = `${x}px`;
      el.style.top = `${y}px`;
      el.style.setProperty("--vcolor", colors[v.trip_id] || "#3fb950");
      el.style.setProperty("--angle",
        `${Math.atan2(pt.y - y, pt.x - x) * 180 / Math.PI}deg`);
      el.textContent = asText(MODE_EMOJI[modeOf[v.trip_id]] ?? DEFAULT_EMOJI);
      el.title = `${v.route ?? ""} · ${v.headsign ?? ""} · `
               + `${v.estimated ? "~" : ""}${v.minutes} min — show on map`;
      el.addEventListener("click", () => {
        cameraMove(() => map.easeTo({ center: [v.lon, v.lat], duration: 600 }));
        pingVehicle(v.trip_id);
      });
      frag.appendChild(el);
    }
    holder.replaceChildren(frag);
  }

  // --- first-run coaching --------------------------------------------------
  // The drag-me-to-a-destination pin is the app's least discoverable
  // control: it does nothing on tap, only on drag. So keep pointing at it
  // — every time a map appears, and whenever the control itself appears —
  // until the rider has actually dragged the goose once. That one success
  // is remembered for good (localStorage); the × only silences it for the
  // current page view.
  const PIN_LEARNED = "honkerPinDragged";
  let coachDismissed = false;
  // The live tip's own placer, so the toolbar can ask it to move over when
  // journey buttons appear beside the goose.
  let coachPlace = null;
  function coachDropPin() {
    if (localStorage.getItem(PIN_LEARNED) || coachDismissed) return;
    if (document.querySelector(".coach")) return;      // already showing
    const btn = document.querySelector(".drop-pin");
    const wrap = $("map-wrap");
    // offsetParent is null while the control is hidden (no origin to plan
    // from) — nothing to point at yet; syncChrome calls back when it shows.
    if (!btn || !wrap || btn.offsetParent === null) return;

    const tip = document.createElement("div");
    tip.className = "coach";
    tip.innerHTML = '<span>Drag this to your destination</span>'
                  + '<button type="button" class="coach-x" '
                  + 'aria-label="Got it">&times;</button>';
    document.body.appendChild(tip);

    const place = () => {
      const b = btn.getBoundingClientRect();
      // Beside the button on its row, arrow pointing back at it — unless
      // journey buttons have appeared in that space, in which case it
      // drops below the toolbar rather than sitting on top of them.
      const busy = ($("tb-solutions")?.children.length || 0) > 0;
      tip.classList.toggle("below", busy);
      if (busy) {
        tip.style.left = `${b.left + b.width / 2}px`;
        tip.style.top = `${b.bottom + 12}px`;
      } else {
        tip.style.left = `${b.right + 12}px`;
        tip.style.top = `${b.top + b.height / 2}px`;
      }
    };
    place();
    coachPlace = place;
    const close = () => {
      window.removeEventListener("resize", place);
      coachPlace = null;
      tip.remove();
    };
    window.addEventListener("resize", place);
    tip.querySelector(".coach-x").addEventListener("click", (ev) => {
      ev.stopPropagation();
      coachDismissed = true;        // quiet for this page view only
      close();
    });
    // Taking the hint hides it; whether the lesson STUCK is decided by the
    // drop handler, which sets PIN_LEARNED only on a real drag.
    btn.addEventListener("pointerdown", close, { once: true });
  }

  // --- the rider themself -------------------------------------------------
  // A live GPS dot with a compass wedge, on every map view. Geolocation is
  // HTTPS-gated off localhost, so this whole feature arms only in secure
  // contexts; the wedge appears once the device supplies a heading.
  let meMarker = null, meWedge = null, mePending = null, meWatch = null;
  function placeMe(ll) {
    if (!mapReady) { mePending = ll; return; }
    if (!meMarker) {
      const el = document.createElement("div");
      el.className = "me-marker";
      meWedge = document.createElement("div");
      meWedge.className = "me-wedge";
      meWedge.hidden = true;
      const dot = document.createElement("div");
      dot.className = "me-dot";
      el.append(meWedge, dot);
      // rotationAlignment map: the wedge keeps pointing the compass way
      // even if the map is ever rotated.
      meMarker = new maplibregl.Marker({ element: el, rotationAlignment: "map" })
        .setLngLat(ll).addTo(map);
    } else {
      meMarker.setLngLat(ll);
    }
  }
  // The map may finish loading after the first GPS fix — map-init calls
  // this from its load handler.
  function flushMeMarker() {
    if (mePending) { const p = mePending; mePending = null; placeMe(p); }
  }
  function startMeMarker(fromGesture) {
    if (!("geolocation" in navigator) || !window.isSecureContext) return;
    if (meWatch == null) {
      meWatch = navigator.geolocation.watchPosition(
        (pos) => placeMe([pos.coords.longitude, pos.coords.latitude]),
        () => {},
        { enableHighAccuracy: true, maximumAge: 5000, timeout: 20000 });
      const onOri = (e) => {
        // iOS names it webkitCompassHeading; the standard event gives alpha
        // (counter-clockwise from north), trusted only when absolute.
        const h = e.webkitCompassHeading != null ? e.webkitCompassHeading
                : (e.absolute && e.alpha != null ? 360 - e.alpha : null);
        if (h != null && meMarker) {
          meWedge.hidden = false;
          meMarker.setRotation(h);
        }
      };
      window.addEventListener("deviceorientationabsolute", onOri, true);
      window.addEventListener("deviceorientation", onOri, true);
    }
    // iOS compass events need a permission that can only be requested
    // during a user gesture — the near-me tap qualifies.
    if (fromGesture && typeof DeviceOrientationEvent !== "undefined"
        && DeviceOrientationEvent.requestPermission) {
      DeviceOrientationEvent.requestPermission().catch(() => {});
    }
  }
