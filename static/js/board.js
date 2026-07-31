// departures board rendering
// Split from index.html; these files load in order as classic
// scripts and share one global scope — boot.js must stay last.
  // --- departures board --------------------------------------------------
  let lastData = null;

  // How many rows fit between the top of the board and the bottom of the
  // viewport. Null until a row has been measured — the first render draws
  // everything, measures, then redraws trimmed.
  let rowHeight = null;

  function fittingRowCount() {
    if (!rowHeight) return null;
    const top = $("board").getBoundingClientRect().top;
    const avail = window.innerHeight - top - BOARD_BOTTOM_GUTTER;
    return Math.max(1, Math.floor(avail / rowHeight));
  }
  const BOARD_BOTTOM_GUTTER = 28;   // breathing room + the status line

  function renderBoard(data) {
    // In a pinned session the SEARCHED ADDRESS stays as the title — it is
    // the user's anchor; the chosen stop is visible on the map and board.
    $("stop-name").textContent =
      (pinParam && pinLabel) ? pinLabel : data.stop.stop_name;
    const board = $("board");
    board.innerHTML = "";
    // Which stop these arrivals belong to — its glyph and name. Matters most
    // in a pinned session, where the titlebar keeps the searched address.
    const head = document.createElement("div");
    head.className = "board-head";
    const bhIcon = document.createElement("span");
    bhIcon.className = "bh-icon";
    bhIcon.textContent = asText(stopGlyph(data));
    const bhName = document.createElement("span");
    bhName.textContent = data.stop.stop_name;
    head.append(bhIcon, bhName);
    // In a pinned session the stop card is dismissible: ✕ drops the stop
    // and returns to browsing the address (same page shape openPinned
    // builds). Without a pin there is nothing to fall back to.
    if (pinParam) {
      const close = document.createElement("button");
      close.type = "button";
      close.className = "bh-close";
      close.title = "Close this stop";
      close.textContent = "×";   // ×, as the search box's close uses
      close.addEventListener("click", () => {
        location.href = `?region=${encodeURIComponent(region)}`
          + `&at=${pinRaw},16&pin=${pinRaw}`
          + `&pinlabel=${encodeURIComponent(pinLabel || "Your address")}`;
      });
      head.appendChild(close);
    }
    board.appendChild(head);
    if (data.departures.length === 0) {
      board.insertAdjacentHTML("beforeend",
        `<div class="empty">No services in the next 90 minutes.</div>`);
    }

    // Show only what fits, and cut the map to match: a marker with no row on
    // screen is exactly the mismatch the vehicles array exists to prevent.
    const limit = fittingRowCount();
    const shown = limit ? data.departures.slice(0, limit) : data.departures;
    const shownIds = new Set(shown.map((d) => d.trip_id));
    const vehicles = (data.vehicles || []).filter((v) => shownIds.has(v.trip_id));
    const ghosts = (data.ghosts || []).filter((g) => shownIds.has(g.trip_id));

    // One colour per departure, shared by the row and — where the service has
    // a live position — its map marker.
    const colors = assignColors(shown);
    const trackedTrips = new Set(vehicles.map((v) => v.trip_id));
    const ghostTrips = new Set(ghosts.map((g) => g.trip_id));
    data.__colors = colors;
    data.__vehicles = vehicles;
    data.__ghosts = ghosts;
    data.__hidden = data.departures.length - shown.length;

      const MODES = { 0: "Tram", 1: "Metro", 2: "Train", 3: "Bus", 4: "Ferry" };
      shown.forEach((d) => {
        const row = document.createElement("div");
        const vcolor = colors[d.trip_id];
        const isTracked = trackedTrips.has(d.trip_id);
        const isGhost = ghostTrips.has(d.trip_id);
        // Every row can show its route; a live or estimated one has a marker to
        // ping. (isTracked is kept as its own class for possible styling.)
        row.className = "row" + (isTracked ? " tracked" : "") + (isGhost ? " ghost" : "");
        if (d.trip_id === selectedTrip) row.classList.add("selected");
        row.dataset.trip = d.trip_id;
        row.style.setProperty("--vcolor", vcolor);
        row.tabIndex = 0;
        const activate = () => {
          selectRoute(d.trip_id);
          if ((isTracked || isGhost) && selectedTrip) pingVehicle(d.trip_id);
        };
        row.addEventListener("click", activate);
        row.addEventListener("keydown", (ev) => {
          if (ev.key === "Enter" || ev.key === " ") { ev.preventDefault(); activate(); }
        });
        // The badge plate stays black; the glyph and number take the service's
        // colour, matching the row stripe and, when tracked, the map marker.
        const badgeStyle = `style="color:${vcolor}"`;
        const routeLabel = d.route ?? "";
        const numClass = routeLabel.length > 8 ? "num xwide"
                       : routeLabel.length > 4 ? "num wide" : "num";
        const glyph = MODE_EMOJI[d.route_type] ?? DEFAULT_EMOJI;
        // "~" marks a timetable-derived countdown, same mark as the map's
        // estimated labels. (The timeline stays bare — a tilde on every
        // stop time would be noise; its header 🛜/📅 says it once.)
        const eta = d.minutes <= 0
          ? `<div class="due">DUE</div>`
          : `<div class="num">${d.realtime ? "" : "~"}${d.minutes}</div>` +
            `<div class="unit">MIN</div>`;
        const bits = [MODES[d.route_type] ?? ""];
        if (d.platform) bits.push(`Platform ${d.platform}`);
        bits.push(new Date(d.predicted * 1000)
          .toLocaleTimeString([], {hour: "2-digit", minute: "2-digit",
                                   ...(regionTz ? {timeZone: regionTz} : {})}));
        row.innerHTML = `
          <div class="badge" ${badgeStyle}>
            <span class="mode">${asText(glyph)}</span>
            <span class="${numClass}">${routeLabel}</span>
          </div>
          <div class="dest">
            <div class="headsign">${d.headsign ?? ""}</div>
            <div class="sub">${bits.filter(Boolean).join(" · ")}</div>
          </div>
          <div class="src-col">
            <span title="${d.realtime ? "Live Location Feed" : "Scheduled/Estimated"}"
                  >${asText(d.realtime ? MARK_LIVE : MARK_SCHEDULED)}</span>
            ${d.alert_ids && d.alert_ids.length
              ? `<button class="alert-mark" title="Service disruption — click for details"
                         aria-label="Service disruption details">${asText(MARK_ALERT)}</button>`
              : ""}
          </div>
          <div class="eta">${eta}</div>`;
        // The ⚠ opens the disruption popup; it must not also select the row.
        const am = row.querySelector(".alert-mark");
        if (am) am.addEventListener("click", (ev) => {
          ev.stopPropagation();
          showAlerts(d.alert_ids);
        });
        board.appendChild(row);
      });

    // Measure once from a real row, then redraw now that the count is known.
    if (!rowHeight) {
      const first = board.querySelector(".row");
      if (first) {
        rowHeight = first.getBoundingClientRect().height;
        renderBoard(data);
        return;
      }
    }

    // The render wiped #board, and the selected trip's stop list with it.
    lastData = data;                  // renderTripPanel reads lastData
    renderTripPanel();

    const age = data.realtime_feed_age;
    // Three honest states: no feeds configured for this region (no key —
    // waiting would never end), configured but not yet polled, or live.
    const feed = data.realtime_configured === false
      ? "timetable only — no realtime feed configured for this region"
      : age === null
      ? "realtime feed: waiting for first fetch"
      : `realtime feed updated ${age}s ago`;
    $("status").textContent = data.__hidden > 0
      ? `${feed} · ${data.__hidden} more not shown`
      : feed;
  }

  async function refresh() {
    if (hasDest() && (stopId || pinnedBrowse)) return refreshPlan();
    if (!stopId) return;
    try {
      const res = await fetch(api(`/departures/${stopId}`));
      if (!res.ok) throw new Error((await res.json()).detail || res.status);
      const data = await res.json();
      renderBoard(data);
      lastData = data;
      updateMap(data);
      if (selectedTrip) refreshTripTimes(selectedTrip);
    } catch (err) {
      $("board").innerHTML = `<div class="error">${err.message}</div>`;
    }
  }
