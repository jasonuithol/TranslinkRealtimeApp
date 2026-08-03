// journey planner: fetch, cards, route drawing
// Split from index.html; these files load in order as classic
// scripts and share one global scope — boot.js must stay last.
  // --- planner rendering ---------------------------------------------------
  const planClock = (epoch) => {
    try {
      return new Intl.DateTimeFormat("en-AU", {
        hour: "2-digit", minute: "2-digit", hour12: false,
        timeZone: regionTz || undefined,
      }).format(epoch * 1000);
    } catch { return new Date(epoch * 1000).toTimeString().slice(0, 5); }
  };

  // "More" journeys: how many extra, later alternates the user has asked
  // for beyond the planner's own picks. Refetched on every poll so they
  // re-cost with fresh realtime exactly like the main cards.
  let planExtra = 0, planExtraFor = null, planMoreDry = false;
  let openLeg = null;         // "itin:leg" whose timetable slice is unfolded
  let planScrolledFor = null; // destKey already scrolled into view (phones)

  async function refreshPlan() {
    try {
      if (planExtraFor !== destKey()) {   // a new destination starts clean
        planExtra = 0; planExtraFor = destKey(); planMoreDry = false;
        openLeg = null;
      }
      // Origin: the pinned ADDRESS whenever one exists — trips start at the
      // departure address, and a stop that later lands in the URL (clicking
      // a landmark writes ?stop=) must not hijack the origin on reload.
      // Only a plan with no pin at all starts from the viewed stop.
      const origin = pinParam
        ? `from_lat=${pinParam.lat}&from_lon=${pinParam.lon}`
          + `&from_label=${encodeURIComponent(pinLabel || "Your address")}`
        : `from=${encodeURIComponent(stopId)}`;
      const dest = toId ? `to=${encodeURIComponent(toId)}`
        : `to_lat=${toPoint.lat}&to_lon=${toPoint.lon}`
          + `&to_label=${encodeURIComponent(toName || "Your destination")}`;
      const res = await fetch(api(`/plan?${origin}&${dest}`));
      if (!res.ok) throw new Error((await res.json()).detail || res.status);
      planData = await res.json();
      // Re-cost the started journey: same anchor time -> same legs, with
      // whatever realtime says about them NOW. No match (timetable swapped
      // under us): its last known copy stays frozen rather than vanishing.
      if (startedPlan) {
        try {
          const res2 = await fetch(api(
            `/plan?${origin}&${dest}&at=${startedPlan.at}`));
          if (res2.ok) {
            const d2 = await res2.json();
            const m = d2.itineraries.find((x) => itinKey(x) === startedPlan.key);
            if (m) { startedPlan.itin = m; saveStarted(); }
          }
        } catch { /* keep the frozen copy */ }
      }
      // Each extra slot re-plans anchored one minute past the last shown
      // departure and keeps the first itinerary not already on the board.
      for (let i = 0; i < planExtra; i++) {
        const lastIt = planData.itineraries[planData.itineraries.length - 1];
        if (!lastIt) { planExtra = i; planMoreDry = true; break; }
        const rx = await fetch(api(
          `/plan?${origin}&${dest}&at=${lastIt.depart + 60}`));
        if (!rx.ok) break;
        const dx = await rx.json();
        const have = new Set(planData.itineraries.map(itinKey));
        const alt = dx.itineraries.find((x) => !have.has(itinKey(x)));
        if (!alt) { planExtra = i; planMoreDry = true; break; }
        planData.itineraries.push(alt);
      }
      if (planData.to && (!toName || toName === toId)) {
        toName = planData.to.stop_name;
        syncPlanUrl();
        syncChrome();
      }
      // Planner mode owns the map: no departures poll, so no vehicles.
      if (lastData) { lastData.__vehicles = []; lastData.__ghosts = []; }
      renderPlan();
      drawPlan();
      // On a phone the fresh cards can render off-screen (the user was
      // down at the map dropping the pin): scroll them into view ONCE per
      // destination — never on the 15 s poll.
      if (planScrolledFor !== destKey()) {
        planScrolledFor = destKey();
        if (!window.matchMedia("(min-width: 900px)").matches) {
          // Instant, and again on the next frame: the map unhiding right
          // after first render changes the document height, which cancels
          // an in-flight smooth animation (observed — the scroll died at
          // whatever pixel the reflow landed on).
          const toCard = () => document.querySelector(".itin")
            ?.scrollIntoView({ block: "start" });
          toCard();
          setTimeout(toCard, 350);
        }
      }
      const risky = planData.itineraries.filter((i) => i.at_risk).length;
      $("status").textContent = "journey plan · refreshed just now"
        + (risky ? ` · ⚠ ${risky} connection${risky > 1 ? "s" : ""} at risk` : "");
    } catch (err) {
      $("board").innerHTML = `<div class="error">${err.message}</div>`;
    }
  }

  function renderPlan() {
    const board = $("board");
    board.innerHTML = "";
    const head = document.createElement("div");
    head.className = "board-head";
    const bhName = document.createElement("span");
    bhName.textContent =
      `${planData.from.stop_name} → ${toName || planData.to.stop_name}`;
    head.append(bhName);
    board.appendChild(head);
    // The started journey (if any) is pinned first — a fresh plan may no
    // longer contain it once its departure has passed.
    const fresh = planData.itineraries.filter(
      (it) => !startedPlan || itinKey(it) !== startedPlan.key);
    const shown = startedPlan ? [startedPlan.itin, ...fresh]
                              : planData.itineraries;
    planData.__shown = shown;
    if (selItin >= shown.length) selItin = 0;
    if (!shown.length) {
      board.insertAdjacentHTML("beforeend",
        `<div class="empty">No journeys found from here today.</div>`);
      return;
    }
    // Each journey gets its OWN colour pool (a walk is a vehicle here
    // too), so no number of "More" cards can exhaust the palette. The
    // bookend walks match their pins: the walk FROM the green departure
    // pin is green, the walk TO the red destination pin is that pin's
    // red. Rides and transfer walks draw from the rest of the pool (red
    // and green reserved, so nothing collides with the bookends). Keys
    // are per-card; the map reads the selected card's keys.
    const GREEN_IDX = VEHICLE_COLORS.indexOf("#2fd07a");
    const RED_IDX = VEHICLE_COLORS.indexOf("#ff5a52");
    planData.__colors = {};
    shown.forEach((it, i) => {
      const used = new Set([GREEN_IDX, RED_IDX]);
      it.legs.forEach((leg, j) => {
        const key = leg.kind === "ride" ? `${i}:${leg.trip_id}`
                                        : `walk:${i}:${j}`;
        if (leg.kind === "walk" && j === 0) {
          planData.__colors[key] = "#2fd07a";        // the departure pin's green
          return;
        }
        if (leg.kind === "walk" && j === it.legs.length - 1) {
          planData.__colors[key] = "#e5484d";        // the destination pin's red
          return;
        }
        const idx = pickFreeIndex(used);
        used.add(idx);
        planData.__colors[key] = VEHICLE_COLORS[idx];
      });
    });
    shown.forEach((it, i) => {
      const card = document.createElement("div");
      card.className = "itin" + (i === selItin ? " selected" : "");
      card.tabIndex = 0;
      const pick = () => { selItin = i; planFitted = false; renderPlan(); drawPlan(); };
      card.addEventListener("click", pick);
      card.addEventListener("keydown", (ev) => {
        if (ev.key === "Enter" || ev.key === " ") { ev.preventDefault(); pick(); }
      });
      // Countdown to set-off (departure minus any leading walk), top right —
      // the same object as an arrivals row's countdown, in the colour of
      // the first thing you must catch (or start walking for).
      let setOff = it.depart;
      if (it.legs.some((l) => l.kind === "ride")) {
        for (const l of it.legs) {
          if (l.kind === "ride") break;
          setOff -= l.secs;
        }
      }   // walk-only: depart already IS the set-off
      const etaMins = Math.round((setOff - Date.now() / 1000) / 60);
      // The journey being FOLLOWED — started, else the selected card — is
      // the "next thing": honk when its set-off is a minute away.
      if (startedPlan ? itinKey(it) === startedPlan.key : i === selItin) {
        maybeHonk(`plan:${itinKey(it) || `walk:${destKey()}`}`, etaMins);
      }
      const firstLeg = it.legs[0];
      const firstColor = planData.__colors[
        firstLeg.kind === "ride" ? `${i}:${firstLeg.trip_id}`
                                 : `walk:${i}:0`] || "#ffb400";
      const eta = document.createElement("div");
      eta.className = "eta it-eta";
      eta.style.setProperty("--vcolor", firstColor);
      eta.innerHTML = etaMins <= 0
        ? `<div class="due">DUE</div>`
        : `<div class="num">${etaMins}</div><div class="unit">MIN</div>`;

      const transfers = it.transfers === 0 ? "direct"
        : it.transfers === 1 ? "1 transfer" : `${it.transfers} transfers`;
      const headRow = document.createElement("div");
      headRow.className = "it-head";
      headRow.innerHTML =
        `<span class="it-times">${planClock(it.depart)} → ${planClock(it.arrive)}</span>` +
        `<span class="it-sub">${it.minutes} min · ${transfers}</span>`;
      headRow.appendChild(eta);
      card.appendChild(headRow);
      // The connection warning belongs to the SERVICE that might be missed,
      // not the whole journey — and only the first such leg: once that
      // connection breaks, everything after it is moot.
      const firstRisk = it.legs.findIndex((l) => l.at_risk);
      it.legs.forEach((leg, j) => {
        if (leg.kind === "walk") {
          // A walk is a leg like any other: the 🚶 "vehicle", its own pool
          // colour, the same badge/detail layout as a ride.
          const vcolor = planData.__colors[`walk:${i}:${j}`] || "#8c98a4";
          // Times: a walk before a ride is pinned back from that boarding;
          // a TRAILING walk (to the destination) runs on from the last
          // arrival.
          let end = null, start = null;
          for (let k = j + 1; k < it.legs.length; k++) {
            if (it.legs[k].kind === "ride") { end = it.legs[k].dep; break; }
          }
          if (end != null) {
            start = end - leg.secs;
          } else {
            for (let k = j - 1; k >= 0; k--) {
              if (it.legs[k].kind === "ride") { start = it.legs[k].arr; break; }
            }
            if (start != null) end = start + leg.secs;
          }
          if (end == null && start == null) {
            // A walk-only journey: no ride to pin the clock to — it runs
            // from the moment you set off.
            start = it.depart;
            end = it.depart + leg.secs;
          }
          const mins = Math.max(1, Math.round(leg.secs / 60));
          const row = document.createElement("div");
          row.className = "it-leg";
          row.style.setProperty("--vcolor", vcolor);
          row.innerHTML = `
            <div class="badge" style="color:${vcolor}">
              <span class="mode">${asText("\u{1F6B6}")}</span>
              <span class="num wide">${mins} min</span>
            </div>
            <div class="it-detail">
              <span>${escapeHtml(leg.from_name ?? "")}</span>
            </div>
            <div class="src-col"></div>
            <div class="it-times2">
              <b>${start != null ? planClock(start) : ""}</b>
            </div>`;
          // Tapping a walk leg unfolds the streets it traverses.
          const legKey = `${i}:${j}`;
          row.classList.add("clickable");
          row.addEventListener("click", (ev) => {
            ev.stopPropagation();
            selItin = i;
            openLeg = openLeg === legKey ? null : legKey;
            renderPlan();
            drawPlan();
          });
          card.appendChild(row);
          if (openLeg === legKey) card.appendChild(walkStreets(it, j, vcolor));
          return;
        }
        // A leg is a board row's language: black badge plate with the mode
        // glyph and route in the vehicle colour, 🛜/📅 in the same colour.
        const vcolor = planData.__colors[`${i}:${leg.trip_id}`] || "#3fb950";
        const row = document.createElement("div");
        row.className = "it-leg";
        row.style.setProperty("--vcolor", vcolor);
        const routeLabel = leg.route ?? "";
        const numClass = routeLabel.length > 8 ? "num xwide"
                       : routeLabel.length > 4 ? "num wide" : "num";
        const glyph = MODE_EMOJI[leg.route_type] ?? DEFAULT_EMOJI;
        // Many feeds bake the platform into the stop name already.
        const plat = (leg.board_platform
                      && !/platform|stop [a-z0-9]/i.test(leg.board_name))
          ? ` · Platform ${leg.board_platform}` : "";
        row.innerHTML = `
          <div class="badge" style="color:${vcolor}">
            <span class="mode">${asText(glyph)}</span>
            <span class="${numClass}">${escapeHtml(routeLabel)}</span>
          </div>
          <div class="it-detail">
            <span>${escapeHtml(leg.board_name)}${plat}</span>
            ${j === firstRisk
              ? `<span class="it-risk">⚠ connection at risk</span>` : ""}
          </div>
          <div class="src-col">
            <span title="${leg.realtime ? "Live Location Feed" : "Scheduled/Estimated"}"
                  >${asText(leg.realtime ? MARK_LIVE : MARK_SCHEDULED)}</span>
          </div>
          <div class="it-times2">
            <b>${planClock(leg.dep)}</b>
          </div>`;
        // Tapping a ride leg unfolds the slice of its timetable the
        // passenger actually rides: board stop through alight stop.
        const legKey = `${i}:${j}`;
        row.classList.add("clickable");
        row.addEventListener("click", (ev) => {
          ev.stopPropagation();
          selItin = i;              // a tapped leg also selects its card
          openLeg = openLeg === legKey ? null : legKey;
          renderPlan();
          drawPlan();
        });
        card.appendChild(row);
        if (openLeg === legKey) card.appendChild(legTimeline(leg, vcolor));
      });
      // The journey's arrival — location AND time — is one dedicated row:
      // leg rows describe only departures (a leg's arrival always doubled
      // the next leg's departure).
      const arriveRow = document.createElement("div");
      arriveRow.className = "it-arrive";
      arriveRow.innerHTML =
        `<span>arrive ${escapeHtml(toName || planData.to.stop_name || "")}</span>`
        + `<b>${planClock(it.arrive)}</b>`;
      card.appendChild(arriveRow);

      // Start = commit to this journey (it stops disappearing once its
      // departure passes); the same button on a started journey removes it.
      const isStarted = startedPlan && itinKey(it) === startedPlan.key;
      if (isStarted) card.classList.add("started");
      const act = document.createElement("div");
      act.className = "it-actions";
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "it-start" + (isStarted ? " remove" : "");
      btn.textContent = isStarted ? "Remove" : "Start";
      btn.addEventListener("click", (ev) => {
        ev.stopPropagation();
        if (isStarted) {
          startedPlan = null;
        } else {
          startedPlan = { region, toId: destKey(), key: itinKey(it),
                          at: it.depart - 60, itin: it };
        }
        saveStarted();
        selItin = 0;
        planFitted = false;
        renderPlan();
        drawPlan();
      });
      act.appendChild(btn);
      card.appendChild(act);
      board.appendChild(card);
    });
    // The selected journey's stops, listed: a tap opens that stop's
    // arrivals board (the plan clears; the pinned address stays).
    const selIt = shown[selItin];
    const jStops = [];
    if (selIt) {
      const seen = new Set();
      for (const leg of selIt.legs) {
        if (leg.kind !== "ride") continue;
        for (const [sid, nm] of [[leg.board, leg.board_name],
                                 [leg.alight, leg.alight_name]]) {
          if (sid && !seen.has(sid)) {
            seen.add(sid);
            jStops.push({ sid, nm, rt: leg.route_type, rgn: leg.region });
          }
        }
      }
    }
    if (jStops.length) {
      const sec = document.createElement("div");
      sec.className = "plan-stops";
      sec.innerHTML = `<div class="ps-head">Stops on this journey</div>`;
      for (const st of jStops) {
        const b = document.createElement("button");
        b.type = "button";
        b.className = "ps-row";
        b.innerHTML =
          `<span class="ps-icon">${asText(landmarkGlyph(st.rt))}</span>`
          + `<span class="ps-name">${escapeHtml(st.nm)}</span>`;
        b.addEventListener("click", () => {
          cancelPlan();               // hand over from planner to arrivals
          selectStop(st.sid, st.rgn);
        });
        sec.appendChild(b);
      }
      board.appendChild(sec);
    }
    // "More": one extra, later journey under the current cards. Goes quiet
    // once a fetch comes back with nothing new — the timetable dried up.
    if (shown.length) {
      const more = document.createElement("button");
      more.type = "button";
      more.className = "plan-more";
      if (planMoreDry) {
        more.disabled = true;
        more.textContent = "No later journeys";
      } else {
        more.textContent = "More";
        more.addEventListener("click", async () => {
          more.disabled = true;
          more.textContent = "Looking…";
          planExtra += 1;
          await refreshPlan();   // re-renders the board, button included
        });
      }
      board.appendChild(more);
    }
  }

  // A walk leg's endpoints: the previous ride's alight (or the journey's
  // origin) to the next ride's board (or the destination) — the same pairs
  // drawPlan routes between.
  function walkEndpoints(it, j) {
    let a = null, b = null;
    for (let k = j - 1; k >= 0; k--) {
      if (it.legs[k].kind === "ride") {
        a = [it.legs[k].alight_lon, it.legs[k].alight_lat]; break;
      }
    }
    if (!a && planData.from && planData.from.stop_lat != null) {
      a = [planData.from.stop_lon, planData.from.stop_lat];
    }
    for (let k = j + 1; k < it.legs.length; k++) {
      if (it.legs[k].kind === "ride") {
        b = [it.legs[k].board_lon, it.legs[k].board_lat]; break;
      }
    }
    if (!b && planData.to && planData.to.stop_lat != null) {
      b = [planData.to.stop_lon, planData.to.stop_lat];
    }
    return a && b && a[0] != null && b[0] != null ? [a, b] : null;
  }

  // Streets a walk traverses, from /api/walkroute (named server-side by the
  // nearest G-NAF address to each path sample). Same endpoint the map's
  // walk lines come from, so the response is usually already server-cached.
  const walkStreetsCache = new Map();
  function walkStreets(it, j, vcolor) {
    const box = document.createElement("div");
    box.className = "leg-tt";
    box.style.setProperty("--vcolor", vcolor);
    box.addEventListener("click", (ev) => ev.stopPropagation());
    const eps = walkEndpoints(it, j);
    if (!eps) {
      box.innerHTML = `<div class="leg-tt-note">No route detail for this walk.</div>`;
      return box;
    }
    const [a, b] = eps;
    const key = `${a[0].toFixed(5)},${a[1].toFixed(5)}:${b[0].toFixed(5)},${b[1].toFixed(5)}`;
    const hit = walkStreetsCache.get(key);
    if (hit === undefined) {
      walkStreetsCache.set(key, null);   // one fetch per pair
      fetch(`/api/walkroute?from_lat=${a[1]}&from_lon=${a[0]}`
            + `&to_lat=${b[1]}&to_lon=${b[0]}`)
        .then((r) => (r.ok ? r.json() : null))
        .then((d) => { walkStreetsCache.set(key, d?.streets || []); renderPlan(); })
        .catch(() => walkStreetsCache.set(key, []));
    }
    if (hit == null) {
      box.innerHTML = `<div class="leg-tt-note">Tracing the walk…</div>`;
      return box;
    }
    if (!hit.length) {
      box.innerHTML = `<div class="leg-tt-note">No street names along this walk.</div>`;
      return box;
    }
    const fmt = (m) => m < 950 ? `${m} m` : `${(m / 1000).toFixed(1)} km`;
    box.innerHTML = hit.map((st) =>
      `<div class="leg-stop"><span class="ls-name">${escapeHtml(st.name)}</span>`
      + `<span class="ls-time">${fmt(st.m)}</span></div>`).join("");
    return box;
  }

  // The ridden slice of a leg's timetable: stops from board to alight,
  // live times where the feed has them, schedule anchored to the leg's own
  // scheduled boarding otherwise (hours can exceed 24:00 after midnight).
  function legTimeline(leg, vcolor) {
    const box = document.createElement("div");
    box.className = "leg-tt";
    box.style.setProperty("--vcolor", vcolor);
    box.addEventListener("click", (ev) => ev.stopPropagation());
    const stops = tripStopsCache.get(leg.trip_id);
    if (stops === undefined) {
      ensureTripStops(leg.trip_id);     // re-renders the plan on arrival
      refreshTripTimes(leg.trip_id);
      box.innerHTML = `<div class="leg-tt-note">Loading stops…</div>`;
      return box;
    }
    if (!stops) {
      box.innerHTML = `<div class="leg-tt-note">No stop list for this service.</div>`;
      return box;
    }
    const bi = stops.findIndex((x) => x.stop_id === leg.board);
    const ai = stops.findIndex((x) => x.stop_id === leg.alight);
    if (bi < 0 || ai <= bi) {
      box.innerHTML = `<div class="leg-tt-note">No stop list for this service.</div>`;
      return box;
    }
    refreshTripTimes(leg.trip_id);      // freshen while open; cache renders
    const lt = (tripTimesCache.get(leg.trip_id) || {}).stops || {};
    const hmsSecs = (h) => h.split(":").reduce((a, x) => a * 60 + +x, 0);
    const mid = leg.sched_dep - hmsSecs(stops[bi].sched);
    box.innerHTML = stops.slice(bi, ai + 1).map((x, idx) => {
      const live = lt[x.stop_id];
      const t = (live && !live.skipped && live.t) ? live.t
              : mid + hmsSecs(x.sched);
      const cls = "leg-stop" + (idx === 0 || idx === ai - bi ? " end" : "");
      return `<div class="${cls}">`
        + `<span class="ls-name">${escapeHtml(x.stop_name)}</span>`
        + `<span class="ls-time">${live && live.skipped
            ? "skipped" : planClock(t)}</span></div>`;
    }).join("");
    return box;
  }

  let destMarker = null;
  function clearPlanLayers() {
    if (!mapReady) return;
    for (const src of ["routes", "vehicles", "ghosts", "landmarks",
                       "walklines", "walklabels"]) {
      const s = map.getSource(src);
      if (s) s.setData(emptyFC());
    }
    if (destMarker) { destMarker.remove(); destMarker = null; }
  }

  // Walk geometry cache: rounded endpoints -> route points from the local
  // walking graph, or null (no graph / no coverage) = draw a straight dash.
  const walkRouteCache = new Map(), walkRoutePending = new Set();
  const walkKey = (a, b) =>
    [a, b].map((p) => p.map((x) => x.toFixed(5)).join(",")).join("|");
  async function ensureWalkRoute(a, b) {
    const key = walkKey(a, b);
    if (walkRouteCache.has(key) || walkRoutePending.has(key)) return;
    walkRoutePending.add(key);
    try {
      const res = await fetch(
        `/api/walkroute?from_lat=${a[1]}&from_lon=${a[0]}`
        + `&to_lat=${b[1]}&to_lon=${b[0]}`);
      // keep the whole answer: points draw the line, streets label it
      walkRouteCache.set(key, res.ok ? await res.json() : null);
      if (planData) drawPlan();
    } catch {
      walkRouteCache.set(key, null);
    } finally {
      walkRoutePending.delete(key);
    }
  }

  // Cut a trip's full shape down to the ridden slice: nearest shape vertex
  // to the boarding stop, then nearest vertex to the alighting stop AFTER it
  // (shapes follow travel direction — searching forward keeps loops honest).
  function sliceShapeBetween(pts, a, b) {
    const coslat = Math.cos((a[1] * Math.PI) / 180) ** 2;
    const nearest = (p, from) => {
      let bi = from, bd = Infinity;
      for (let i = from; i < pts.length; i++) {
        const dx = pts[i][0] - p[0], dy = pts[i][1] - p[1];
        const d = dx * dx * coslat + dy * dy;
        if (d < bd) { bd = d; bi = i; }
      }
      return bi;
    };
    const i = nearest(a, 0);
    const j = nearest(b, i);
    return j > i ? pts.slice(i, j + 1) : null;
  }

  // The selected itinerary on the map: each ride leg drawn along its trip's
  // REAL shape, sliced between board and alight (falling back to
  // stop-to-stop lines while the shape loads, or if the trip has none);
  // transfer points as landmarks, the destination as a green marker.
  function drawPlan() {
    if (!mapReady || !planData || !hasDest()) return;
    const it = (planData.__shown || planData.itineraries)[selItin];
    if (!it) { clearPlanLayers(); return; }
    const lines = [], marks = [], rideLabels = [];
    let waiting = false;
    for (const leg of it.legs) {
      if (leg.kind !== "ride") continue;
      const stops = tripStopsCache.get(leg.trip_id);
      if (!stops) { ensureTripStops(leg.trip_id); waiting = true; continue; }
      const bi = stops.findIndex((s) => s.stop_id === leg.board);
      const ai = stops.findIndex((s) => s.stop_id === leg.alight);
      if (bi < 0 || ai < 0) continue;
      const seg = stops.slice(Math.min(bi, ai), Math.max(bi, ai) + 1);
      let coords = seg.map((s) => [s.lon, s.lat]);
      if (leg.shape_id) {
        const pts = shapeCache.get(leg.shape_id);
        if (pts === undefined) { ensureShape(leg.shape_id, region); waiting = true; }
        else if (pts && pts.length > 1) {
          const sliced = sliceShapeBetween(
            pts, coords[0], coords[coords.length - 1]);
          if (sliced && sliced.length > 1) coords = sliced;
        }
      }
      lines.push({
        type: "Feature",
        geometry: { type: "LineString", coordinates: coords },
        // Same vehicle colour as the leg's badge — board and map agree.
        properties: { color: (planData.__colors || {})[`${selItin}:${leg.trip_id}`]
                              || "#3fb950" },
      });
      // The ridden slice wears its street names, like the walks do.
      const rideSts = streetsFor(coords, () => { if (planData) drawPlan(); });
      rideLabels.push(...streetLabelFeatures(rideSts));
      for (const end of [seg[0], seg[seg.length - 1]]) {
        marks.push({
          type: "Feature",
          geometry: { type: "Point", coordinates: [end.lon, end.lat] },
          properties: {
            stop_id: end.stop_id, name: end.stop_name,
            icon: ensureVehicleIcon(landmarkGlyph(end.route_type), LANDMARK_INK),
          },
        });
      }
    }
    map.getSource("routes").setData({ type: "FeatureCollection", features: lines });
    // (rideLabels joins the walk labels in the walklabels source below.)
    map.getSource("landmarks").setData({ type: "FeatureCollection", features: marks });
    map.getSource("vehicles").setData(emptyFC());
    map.getSource("ghosts").setData(emptyFC());

    // Walks are the gaps between rides: origin -> first board, alight ->
    // next board. Drawn dashed along the walking graph, in the SAME pool
    // colour as the walk leg's badge — the walk is a vehicle here.
    const walks = [], walkLabels = [];
    let cursor = (planData.from && planData.from.stop_lat != null)
      ? [planData.from.stop_lon, planData.from.stop_lat] : null;
    let gapWalkIdx = null;
    it.legs.forEach((leg, j) => {
      if (leg.kind === "walk") { if (gapWalkIdx === null) gapWalkIdx = j; return; }
      if (cursor && leg.board_lat != null) {
        const b = [leg.board_lon, leg.board_lat];
        const dm = Math.hypot((b[0] - cursor[0]) * 88000,
                              (b[1] - cursor[1]) * 111000);
        if (dm > 25) {   // sub-25 m "walks" are platform hops, not journeys
          const key = walkKey(cursor, b);
          let wr = walkRouteCache.get(key);
          if (wr === undefined) { ensureWalkRoute(cursor, b); wr = null; }
          walks.push({
            type: "Feature",
            geometry: { type: "LineString",
                        coordinates: wr?.points || [cursor, b] },
            properties: {
              color: gapWalkIdx !== null
                ? (planData.__colors || {})[`walk:${selItin}:${gapWalkIdx}`]
                : undefined,
            },
          });
          for (const st of wr?.streets || []) {
            if (st.line?.length > 1) walkLabels.push({
              type: "Feature",
              geometry: { type: "LineString", coordinates: st.line },
              properties: { name: st.name },
            });
          }
        }
      }
      if (leg.alight_lat != null) cursor = [leg.alight_lon, leg.alight_lat];
      gapWalkIdx = null;
    });
    // A TRAILING walk (last leg, no ride after it) ends at the destination.
    if (gapWalkIdx !== null && cursor && planData.to
        && planData.to.stop_lat != null) {
      const b = [planData.to.stop_lon, planData.to.stop_lat];
      const dm = Math.hypot((b[0] - cursor[0]) * 88000,
                            (b[1] - cursor[1]) * 111000);
      if (dm > 25) {
        const key = walkKey(cursor, b);
        let wr = walkRouteCache.get(key);
        if (wr === undefined) { ensureWalkRoute(cursor, b); wr = null; }
        walks.push({
          type: "Feature",
          geometry: { type: "LineString", coordinates: wr?.points || [cursor, b] },
          properties: {
            color: (planData.__colors || {})[`walk:${selItin}:${gapWalkIdx}`],
          },
        });
        for (const st of wr?.streets || []) {
          if (st.line?.length > 1) walkLabels.push({
            type: "Feature",
            geometry: { type: "LineString", coordinates: st.line },
            properties: { name: st.name },
          });
        }
      }
    }
    map.getSource("walklines").setData(
      { type: "FeatureCollection", features: walks });
    map.getSource("walklabels").setData(
      { type: "FeatureCollection", features: [...rideLabels, ...walkLabels] });
    if (planData.to
        && planData.to.stop_lat != null && planData.to.stop_lon != null) {
      const dll = [planData.to.stop_lon, planData.to.stop_lat];
      if (!destMarker) {
        // RED, the arrival end (the departure pin is green). Draggable:
        // dropping it elsewhere re-plans to the new spot.
        destMarker = new maplibregl.Marker(
          { element: goosePin("dest"), anchor: "bottom", draggable: true })
          .setLngLat(dll).addTo(map);
        destMarker.on("dragend", () => {
          const ll = destMarker.getLngLat();
          setDestPoint(ll.lat, ll.lng, "dropped pin");
        });
      } else {
        destMarker.setLngLat(dll);   // a changed destination moves it
      }
    }
    if (!planFitted && lines.length && !waiting) {
      const b = new maplibregl.LngLatBounds();
      for (const l of lines) for (const c of l.geometry.coordinates) b.extend(c);
      if (pinParam) b.extend([pinParam.lon, pinParam.lat]);  // the front door
      cameraMove(() => map.fitBounds(b, { padding: 70, maxZoom: 15.5 }));
      planFitted = true;
    }
  }
