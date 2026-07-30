// disruption alerts
// Split from index.html; these files load in order as classic
// scripts and share one global scope — boot.js must stay last.
  // --- disruption alerts ---------------------------------------------------
  // Alert text comes from the feed, so the popup is built with DOM APIs — never
  // innerHTML — to keep any markup in the text inert.
  function showAlerts(ids) {
    const map = lastData?.alerts || {};
    const box = $("alert-box");
    box.innerHTML = "";
    const h = document.createElement("h2");
    const g = document.createElement("span");
    g.className = "glyph";
    g.textContent = asText(MARK_ALERT);
    h.append(g, " Service disruptions");
    box.appendChild(h);
    ids.forEach((i) => {
      const a = map[String(i)];
      if (!a) return;
      const item = document.createElement("div");
      item.className = "alert-item";
      const eff = document.createElement("div");
      eff.className = "effect";
      eff.textContent = a.effect || "";
      const head = document.createElement("div");
      head.className = "head";
      head.textContent = a.header || "";
      const body = document.createElement("div");
      body.className = "body";
      body.textContent = a.description || "";
      item.append(eff, head, body);
      box.appendChild(item);
    });
    const close = document.createElement("button");
    close.className = "alert-close";
    close.textContent = "Close";
    close.onclick = closeAlerts;
    box.appendChild(close);
    $("alert-modal").hidden = false;
    close.focus();
  }

  function closeAlerts() { $("alert-modal").hidden = true; }

  $("alert-modal").addEventListener("click", (e) => {
    if (e.target === $("alert-modal")) closeAlerts();   // backdrop only
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && !$("alert-modal").hidden) closeAlerts();
  });

  // Map -> board row. Flash the matching row and bring it into view.
  let flashTimer = null;
  function flashRow(tripId) {
    const row = document.querySelector(`.row[data-trip="${CSS.escape(tripId)}"]`);
    if (!row) return;
    clearTimeout(flashTimer);
    document.querySelectorAll(".row.flash").forEach((r) => r.classList.remove("flash"));
    void row.offsetWidth;             // restart the animation if it is re-clicked
    row.classList.add("flash");
    row.scrollIntoView({ block: "nearest", behavior: "smooth" });
    flashTimer = setTimeout(() => row.classList.remove("flash"), 2200);
  }

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
