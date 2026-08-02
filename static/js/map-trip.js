// route drawing, trip selection, the trip stops panel
// Split from index.html; these files load in order as classic
// scripts and share one global scope — boot.js must stay last.
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
    // Stacked (phone) layout: the timeline slots into the flow UNDER the
    // service's own row — the absolute overlay would smother the whole
    // narrow board. Side by side it stays the overlay below the heading.
    const ownRow = !window.matchMedia("(min-width: 900px)").matches
      && board.querySelector(`.row[data-trip="${CSS.escape(selectedTrip)}"]`);
    if (ownRow) ownRow.after(panel);
    else board.appendChild(panel);
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
