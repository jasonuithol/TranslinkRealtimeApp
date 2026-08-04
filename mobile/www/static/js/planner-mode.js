// journey-planner mode: destination state and chips
// Split from index.html; these files load in order as classic
// scripts and share one global scope — boot.js must stay last.
  // --- journey planner mode ------------------------------------------------
  let pickingDest = false;
  let planData = null, selItin = 0, planFitted = false;

  // A STARTED journey is a commitment: it stays on the board after its
  // departure time passes (a fresh plan would no longer return it). It is
  // re-costed each poll by re-planning anchored at its original departure
  // (`at=` — the schedule is deterministic, so the same legs come back
  // with current realtime), and it survives reloads via localStorage.
  let startedPlan = null;
  try {
    const s = JSON.parse(localStorage.getItem("startedJourney") || "null");
    if (s && s.region === region && hasDest() && s.toId === destKey()) {
      startedPlan = s;
    }
  } catch { /* corrupted store: ignore */ }
  const itinKey = (it) => it.legs.filter((l) => l.kind === "ride")
    .map((l) => `${l.trip_id}@${l.board}`).join(">");
  function saveStarted() {
    if (startedPlan) {
      localStorage.setItem("startedJourney", JSON.stringify(startedPlan));
    } else {
      localStorage.removeItem("startedJourney");
    }
  }

  function syncPlanUrl() {
    const p = new URLSearchParams();
    p.set("region", region);
    if (stopId) p.set("stop", stopId);
    if (toId) { p.set("to", toId); }
    else if (toPoint) {
      p.set("tolat", toPoint.lat.toFixed(5));
      p.set("tolon", toPoint.lon.toFixed(5));
    }
    if (hasDest() && toName) p.set("toname", toName);
    if (pinParam) { p.set("pin", pinRaw); if (pinLabel) p.set("pinlabel", pinLabel); }
    history.replaceState(null, "", "?" + p.toString());
  }

  function setDest(sid, name) {
    toId = sid;
    toPoint = null;
    _destChosen(name || sid);
  }

  // "Dropped pin" is not a place a rider can walk to. Ask the server what
  // is actually there (nearest G-NAF address) and use that instead; the
  // fallback stands only where the country has no address to give.
  async function addressFor(lat, lon) {
    try {
      const r = await fetch(api(`/rgeocode?lat=${lat}&lon=${lon}`));
      if (r.ok) return (await r.json()).label || null;
    } catch { /* offline or no G-NAF: keep the caller's wording */ }
    return null;
  }
  const UNNAMED = /^(dropped pin|destination|your address|your destination)$/i;

  // A place or address as destination stays a POINT — the pizza joint IS
  // the destination; the planner walks the last leg to its door.
  function setDestPoint(lat, lon, label) {
    toId = null;
    toPoint = { lat, lon };
    _destChosen(label || "destination");
    if (!label || UNNAMED.test(label)) {
      addressFor(lat, lon).then((addr) => {
        // Ignore a late answer for a pin the user has already moved on from.
        if (!addr || !toPoint || toPoint.lat !== lat || toPoint.lon !== lon) return;
        toName = addr;
        syncPlanUrl();
        syncChrome();
        refresh();          // re-plan so the legs say the address too
      });
    }
  }

  function _destChosen(name) {
    toName = name;
    pickingDest = false;
    planData = null; selItin = 0; planFitted = false;
    $("search").placeholder = "Search for a stop or an address";
    closeSearch();
    syncPlanUrl();
    syncChrome();
    $("board").innerHTML = `<div class="empty">Planning…</div>`;
    refresh();
    // A pinned-address plan has no stop selected, so no poll is running yet.
    clearInterval(timer);
    timer = setInterval(refresh, 15000);
  }

  // A dragged departure pin re-homes the journey: the old address label no
  // longer applies, and the plan (or the surrounding-stop landmarks, when
  // just browsing) re-derives from the new spot.
  function movePin(lat, lon) {
    if (!pinParam) return;
    pinParam.lat = lat; pinParam.lon = lon;
    pinRaw = `${lon.toFixed(5)},${lat.toFixed(5)}`;
    pinLabel = "dropped pin";
    planData = null; planFitted = false;
    syncPlanUrl();
    $("stop-name").textContent = pinLabel;
    addressFor(lat, lon).then((addr) => {
      if (!addr || !pinParam || pinParam.lat !== lat || pinParam.lon !== lon) return;
      pinLabel = addr;
      $("stop-name").textContent = pinLabel;
      syncPlanUrl();
      if (hasDest()) refresh();   // the legs name the origin too
      else renderPinStops();      // ..."Stops near <address>"
    });
    if (hasDest()) {
      $("board").innerHTML = `<div class="empty">Planning…</div>`;
      refresh();
    } else {
      loadPinSurrounds();
      if (stopId) refresh();
    }
  }

  function cancelPlan() {
    toId = null; toPoint = null; toName = ""; planData = null;
    startedPlan = null; saveStarted();
    clearPlanLayers();
    syncPlanUrl();
    syncChrome();
    if (!stopId) {
      // back to the pinned browse: stops on the map, empty board
      clearInterval(timer);
      $("board").innerHTML = `<div class="empty placeholder">
        <div>Select a stop to see departures.</div>
        <div class="ph-icon">&#x1F570;&#xFE0E;</div></div>`;
      drawLandmarks();     // restore the pin's surrounding stops
      renderPinStops();    // ...and their list in the board
      return;
    }
    refresh();
  }

  $("plan-to").addEventListener("click", () => {
    if (hasDest()) { cancelPlan(); return; }
    pickingDest = true;
    $("search").placeholder = "Where to?";
    openSearch();
  });

  $("search").addEventListener("keydown", (e) => {
    if (e.key === "Escape" && (stopId || pinnedBrowse)) closeSearch();
  });
