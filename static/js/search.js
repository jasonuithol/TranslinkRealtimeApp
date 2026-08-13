// stop / address / place search
// Split from index.html; these files load in order as classic
// scripts and share one global scope — boot.js must stay last.
  // --- stop search -------------------------------------------------------
  // Three ways in: stop-name search (a dropdown of stops), address search
  // (geocoded server-side) and "near me" (browser geolocation) — the last
  // two both end at openPinned(): pin on the map, surrounding stops around it.
  const fmtDist = (m) => m < 1000 ? `${m} m` : `${(m / 1000).toFixed(1)} km`;

  function resultButton(html, onclick) {
    const b = document.createElement("button");
    b.innerHTML = html;
    b.onclick = onclick;
    $("results").appendChild(b);
    return b;
  }

  function resultNote(text) {
    const b = document.createElement("button");
    b.textContent = text;
    b.disabled = true;
    b.style.color = "var(--muted)";
    b.style.cursor = "default";
    $("results").appendChild(b);
  }

  // The dropdown must never run past the bottom of the screen, on any device:
  // cap it to the space between its own top edge and the viewport bottom, and
  // let the overflow scroll inside. visualViewport (where available) tracks
  // the on-screen keyboard, which window.innerHeight does not.
  function fitResults() {
    const box = $("results");
    if (box.hidden) return;
    const vh = window.visualViewport ? window.visualViewport.height
                                     : window.innerHeight;
    const top = box.getBoundingClientRect().top;
    box.style.maxHeight = `${Math.max(96, vh - top - 12)}px`;
  }
  if (window.visualViewport) {
    window.visualViewport.addEventListener("resize", fitResults);
  }

  // A picked stop in another region: remember the region and reload straight
  // onto the stop — the map style, caches and colours are all per-network.
  function openInRegion(rid, sid) {
    localStorage.setItem("region", rid);
    location.href =
      `?region=${encodeURIComponent(rid)}&stop=${encodeURIComponent(sid)}`;
  }

  function stopEntry(s) {
    const bits = [];
    if (s.is_station) bits.push("station");
    if (s.dist_m != null) bits.push(fmtDist(s.dist_m));
    bits.push(s.stop_id);
    if (s.state) bits.push(s.state);
    const rid = s.region || region;
    resultButton(
      `<span class="rmain">${escapeHtml(s.stop_name)}` +
        `<span class="sid">${bits.join(" · ")}</span></span>` +
        `<span class="ricon" aria-hidden="true">` +
        `${asText(landmarkGlyph(s.route_type))}</span>`,
      () => {
        if (pickingDest) {
          // Destination pick: same network only (cross-region is phase 3).
          if (rid !== region) {
            resultNote("Cross-region journeys aren't supported yet — "
                       + "pick a destination in this network.");
            return;
          }
          setDest(s.stop_id, s.stop_name);
          return;
        }
        rid === region ? selectStop(s.stop_id) : openInRegion(rid, s.stop_id);
      },
    );
  }

  // Closing a stop's arrivals lets the stop GO: without this the poll kept
  // running, so its marker stayed lit and its buses stayed on the map with
  // no card to explain them. The opposite of selectStop, in place — no
  // reload, so the pinned address and the camera survive.
  function deselectStop() {
    if (!stopId) return;
    stopId = null;
    selectedTrip = null;
    lastData = null;
    clearInterval(timer);
    timer = null;
    if (mapReady) {
      for (const src of ["vehicles", "ghosts", "stop", "routes", "walklabels"]) {
        const s = map.getSource(src);
        if (s) s.setData(emptyFC());
      }
      syncEdgeMarkers();      // the off-screen markers go with them
      drawLandmarks();        // back to the pin's surrounding stops
      syncRailStationFilter();
    }
    renderTripPanel();        // any open timeline closes with it
    $("board").innerHTML = "";
    syncPlanUrl();            // ?stop= leaves the URL
    syncChrome();
    renderPinStops();
  }

  // Stop names and geocoder labels are external data; never trust them as HTML.
  const escapeHtml = (t) => String(t).replace(/[&<>"']/g,
    (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

  // rid narrows the lookup to one region when the caller is CERTAIN the
  // point belongs to it; null asks every region and lets distance sort it
  // out (only the right city returns anything close). Address hits pass
  // null: geocode viewboxes overlap (nsw spans four states), so "the region
  // whose lookup found it" proved to be the wrong region for a Varsity
  // Lakes street that came back via nsw — whose nearest stop is 100 km off.
  async function fetchNearby(lat, lon, rid = null) {
    await regionsReady.catch(() => {});
    const rids = rid ? [{ id: rid, state: stateOf(rid) }] : regionOrder();
    const lists = await Promise.all(rids.map(async (r) => {
      try {
        const res = await fetch(
          `/api/r/${r.id}/stops/nearby?lat=${lat}&lon=${lon}`);
        return (await res.json())
          .map((s) => ({ ...s, region: r.id, state: r.state }));
      } catch { return []; }
    }));
    return lists.flat().sort((a, b) => a.dist_m - b.dist_m);
  }

  // A located point — a clicked address, or "near me" — goes straight to
  // the map: an amber pin on the spot, the surrounding stops visible and
  // clickable (the all-stops layer is live at this zoom). The region is
  // whichever network actually has the NEAREST stop — never the region
  // whose geocoder resolved the address; their viewboxes overlap (see the
  // Varsity-Lakes-NSW incident).
  async function openPinned(lat, lon, what, label) {
    const box = $("results");
    box.innerHTML = "";
    resultNote(`Finding stops near ${what}…`);
    box.hidden = false;
    fitResults();
    let stops = [];
    try { stops = await fetchNearby(lat, lon); } catch { /* fall through */ }
    if (!stops.length) {
      box.innerHTML = "";
      resultNote(`No stops near ${what}.`);
      fitResults();
      return;
    }
    const rid = stops[0].region;
    const lonlat = `${lon.toFixed(5)},${lat.toFixed(5)}`;
    localStorage.setItem("region", rid);
    location.href = `?region=${encodeURIComponent(rid)}`
                  + `&at=${lonlat},16&pin=${lonlat}`
                  + `&pinlabel=${encodeURIComponent(label || what)}`;
  }

  // On a pinned page: fetch the stops around the pin, hand them to the
  // landmarks layer, and fit the camera so the pin AND its stops are all in
  // view — a dense corner stays at street zoom (maxZoom caps it), a sparse
  // suburb zooms out until its stops appear.
  async function loadPinSurrounds() {
    if (!pinParam || !mapReady) return;
    try {
      const near = (await fetchNearby(pinParam.lat, pinParam.lon, region))
        .filter((s, i) => i === 0 || s.dist_m <= 1500)
        .slice(0, 8);
      if (!near.length) return;   // the pin alone is still worth showing
      pinStops = near;
      drawLandmarks();
      flashPinStops();
      renderPinStops();
      if (browsing) {
        const b = new maplibregl.LngLatBounds(
          [pinParam.lon, pinParam.lat], [pinParam.lon, pinParam.lat]);
        for (const s of pinStops) b.extend([s.lon, s.lat]);
        cameraMove(() => map.fitBounds(b, { padding: 80, maxZoom: 16.5 }));
      }
    } catch { /* the pin alone is still worth showing */ }
  }

  // The pinned address's surrounding stops, LISTED in the board as well
  // as drawn on the map — each row opens that stop's arrivals board.
  // No-op outside the pinned-browse state (a stop or a plan owns the
  // board then).
  function renderPinStops() {
    if (!pinParam || stopId || hasDest() || !pinStops.length) return;
    const board = $("board");
    board.innerHTML = "";
    const head = document.createElement("div");
    head.className = "board-head";
    const nm = document.createElement("span");
    nm.textContent = `Stops near ${pinLabel || "your pin"}`;
    head.append(nm);
    board.appendChild(head);
    const sec = document.createElement("div");
    sec.className = "plan-stops";
    for (const s of pinStops) {
      const b = document.createElement("button");
      b.type = "button";
      b.className = "ps-row";
      b.dataset.sid = s.stop_id;
      b.innerHTML =
        `<span class="ps-icon">${asText(landmarkGlyph(s.route_type))}</span>`
        // Stops come in same-named pairs either side of a road. The compass
        // point is which way services LEAVE this one, which is the only
        // thing that tells the pair apart in a list.
        + (s.heading ? `<span class="ps-dir">${escapeHtml(s.heading)}</span>` : "")
        + `<span class="ps-name">${escapeHtml(s.stop_name)}</span>`
        + `<span class="ps-dist">${fmtDist(s.dist_m)}</span>`;
      b.addEventListener("click", () =>
        s.region && s.region !== region
          ? openInRegion(s.region, s.stop_id)
          : selectStop(s.stop_id));
      // List -> map: hovering the row halos the stop out on the map.
      b.addEventListener("mouseenter", () => hoverStop(s.lat, s.lon, s.route_type));
      b.addEventListener("mouseleave", () => hoverStop(null));
      sec.appendChild(b);
    }
    board.appendChild(sec);
  }

  // Append address candidates to the dropdown (below any stop matches).
  // ONE nationwide query, biased to the current region's viewbox — the
  // region a clicked address lands in comes from its nearest STOP, so
  // there is nothing to gain from asking all nine regions, and everything
  // to lose: the server serialises Nominatim at 1 req/s, so the old
  // fan-out took ~9 s per uncached address.
  async function searchAddress(q, { clear = true, signal, stale } = {}) {
    stale = stale || (() => false);
    const box = $("results");
    if (clear) { box.innerHTML = ""; resultNote(`Looking up “${q}”…`); }
    box.hidden = false;
    fitResults();
    try {
      await regionsReady.catch(() => {});
      const bias = searchBias();
      const nearQ = bias
        ? `&near_lat=${bias.lat.toFixed(5)}&near_lon=${bias.lon.toFixed(5)}`
        : "";
      const res = await fetch(api(`/geocode?q=${encodeURIComponent(q)}${nearQ}`),
                              { signal });
      if (stale()) return;   // superseded while the geocoder was working
      if (!res.ok) throw new Error();
      // Nominatim can return near-duplicate rows — one label, one row.
      const seen = new Set();
      const places = (await res.json())
        .map((p) => ({ ...p, region, state: stateOf(region) }))
        .filter((p) => !seen.has(p.label) && seen.add(p.label));
      if (stale()) return;
      if (clear) box.innerHTML = "";
      if (!places.length) {
        if (clear) resultNote(`Nothing found for “${q}”.`);
        return;
      }
      places.forEach((p) => {
        // Just the label ('lead, suburb STATE', built server-side) — no tag:
        // an address row is recognisable by its shape, and the missing stop
        // glyph on the right already tells it apart from a stop.
        resultButton(
          `<span class="rmain">${escapeHtml(p.label)}</span>`,
          () => pickingDest
            ? setDestPoint(p.lat, p.lon, p.label)   // the PLACE is the dest
            : openPinned(p.lat, p.lon, "that address", p.label),
        );
      });
    } catch {
      if (stale()) return;   // an aborted run must not paint an error note
      if (clear) { box.innerHTML = ""; resultNote("Address lookup unavailable right now."); }
    } finally {
      fitResults();
    }
  }

  // A trailing word may still be mid-typing; an address is assumed once the
  // query has a house number or is long enough that no stop matched it.
  const looksLikeAddress = (q) => /^\d+\s+\S/.test(q);

  // Stop-name search fans out over every ingested region; the row's state
  // label (QLD/VIC) is what tells two "Central Station"s apart.
  async function searchStops(q, signal) {
    await regionsReady.catch(() => {});
    const lists = await Promise.all(regionOrder().map(async (r) => {
      try {
        const res = await fetch(
          `/api/r/${r.id}/stops/search?q=${encodeURIComponent(q)}`, { signal });
        return (await res.json())
          .map((s) => ({ ...s, region: r.id, state: r.state }));
      } catch { return []; }
    }));
    return lists.flat();
  }

  let searchDebounce;
  let searchToken = 0;      // bumped on EVERY keypress; in-flight runs bail
  let searchAbort = null;   // …and their fetches are cancelled outright
  $("search").addEventListener("input", (e) => {
    // Remember what was literally typed — an origin search only, not a
    // "Where to?" pick — so "Change address" can replay it on the landing.
    if (!pickingDest) localStorage.setItem("lastTyped", e.target.value.trim());
    clearTimeout(searchDebounce);
    // A new keypress abandons the previous search immediately: its fetches
    // are aborted and any response already in flight is dropped before it
    // can render (the token check) — stale results must never flash in.
    searchToken++;
    if (searchAbort) searchAbort.abort();
    const q = e.target.value.trim();
    if (q.length < 3) { $("results").hidden = true; return; }
    // Fast typists: nothing is searched until the keyboard has been quiet
    // for a full second — it also keeps mid-typing queries away from the
    // shared 1 req/s geocoder.
    searchDebounce = setTimeout(async () => {
      const token = searchToken;
      const stale = () => token !== searchToken;
      const ctl = (searchAbort = new AbortController());
      const stops = await searchStops(q, ctl.signal);
      if (stale()) return;
      const box = $("results");
      box.innerHTML = "";
      stops.forEach(stopEntry);
      // Address lookup happens automatically — nobody should have to say
      // "that was an address": a query starting with a house number is one,
      // and a longer query matching no stop name probably is too.
      if (looksLikeAddress(q) && q.length >= 6) {
        await searchAddress(q, { clear: stops.length === 0,
                                 signal: ctl.signal, stale });
      } else if (!stops.length && q.length >= 6) {
        await searchAddress(q, { signal: ctl.signal, stale });
      }
      if (stale()) return;
      box.hidden = box.children.length === 0;
      fitResults();
    }, 1000);
  });

  // "near me" is only offered where it can actually deliver: the device must
  // have a geolocation API, and the page must be a secure context (https, or
  // localhost during dev) — browsers silently deny geolocation on plain http,
  // so on the bare-IP VPS the button would be a dead end.
  if (!("geolocation" in navigator) || !window.isSecureContext) {
    $("near-me").hidden = true;
  }

  // A quiet fix of the device's location, taken ONLY when permission was
  // already granted (no prompt, cached position is fine) — it biases address
  // search toward home without the user having to say where home is.
  let nearMe = null;
  if ("geolocation" in navigator && window.isSecureContext
      && navigator.permissions?.query) {
    navigator.permissions.query({ name: "geolocation" }).then((p) => {
      if (p.state !== "granted") return;
      navigator.geolocation.getCurrentPosition(
        (pos) => { nearMe = { lat: pos.coords.latitude,
                              lon: pos.coords.longitude }; },
        () => {},
        { enableHighAccuracy: false, timeout: 8000, maximumAge: 300000 });
      startMeMarker(false);   // and keep the rider's dot live on the map
    }).catch(() => {});
  }

  // The point address results should sort around: picking a DESTINATION,
  // it's the journey's departure (the pin, else the viewed stop); picking a
  // departure, it's wherever the device says the user is (else the pin).
  function searchBias() {
    const stopPt = (lastData && lastData.stop
                    && lastData.stop.stop_lat != null)
      ? { lat: lastData.stop.stop_lat, lon: lastData.stop.stop_lon } : null;
    return pickingDest
      ? (pinParam || stopPt || nearMe)
      : (nearMe || pinParam);
  }

  $("near-me").addEventListener("click", () => {
    const box = $("results");
    if (!navigator.geolocation) {
      box.innerHTML = ""; resultNote("This browser has no geolocation."); box.hidden = false;
      return;
    }
    // A user gesture: the one chance iOS gives to request compass events.
    startMeMarker(true);
    box.innerHTML = "";
    resultNote("Locating…");
    box.hidden = false;
    fitResults();
    navigator.geolocation.getCurrentPosition(
      (pos) => openPinned(pos.coords.latitude, pos.coords.longitude, "you",
                          "Near me"),
      () => {
        box.innerHTML = "";
        // Browsers only allow geolocation on HTTPS (localhost excepted).
        resultNote(location.protocol === "http:" && location.hostname !== "localhost"
          ? "Location blocked: geolocation needs HTTPS."
          : "Could not get your location.");
      },
      { enableHighAccuracy: false, timeout: 8000, maximumAge: 60000 },
    );
  });

  function selectStop(id, rgn) {
    // A landmark from a sibling region's traced route: that stop lives in the
    // other region's timetable, so open it there (a full reload — the map
    // style, caches and colour state are all per-network).
    if (rgn && rgn !== region) {
      localStorage.setItem("region", rgn);
      location.href = `?region=${encodeURIComponent(rgn)}&stop=${encodeURIComponent(id)}`;
      return;
    }
    stopId = id;
    shellFreshAsk();          // a new stop: the overlay is wanted again
    selectedTrip = null;      // a route from the previous stop is not relevant
    // There is something to look at now: uncover the map and give it its full
    // height, then tell MapLibre the container changed.
    $("map-empty").hidden = true;
    $("map-caption").hidden = false;
    if ($("map-wrap").classList.contains("awaiting")) {
      $("map-wrap").classList.remove("awaiting");
      if (mapReady) cameraMove(() => map.resize());
    }
    autoFit = true;           // a new stop re-enables auto-fit...
    forceFit = true;          // ...and reframes even if the user had panned
    closeSearch();            // a stop is chosen: fold the search away
    syncRailStationFilter();  // stop drawing the station we now view in grey
    planFitted = false;   // a new origin reframes the journey, if one is set
    syncPlanUrl();        // keeps the address pin AND any planner destination
    refresh();
    clearInterval(timer);
    timer = setInterval(refresh, 15000);
  }
