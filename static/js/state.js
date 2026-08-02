// URL params, shared page state, region config
// Split from index.html; these files load in order as classic
// scripts and share one global scope — boot.js must stay last.
  const $ = (id) => document.getElementById(id);
  let stopId = new URLSearchParams(location.search).get("stop");
  let timer = null;

  // ?at=lon,lat,zoom — a camera handed across a region swap. With no stop it
  // also puts the page into map-BROWSING mode: the map is open (all-stops
  // clickable, board still empty) instead of the landing, so panning from
  // one city into another feels continuous across the provider change.
  const atParam = (() => {
    const raw = new URLSearchParams(location.search).get("at");
    if (!raw) return null;
    const [lon, lat, zoom] = raw.split(",").map(Number);
    return [lon, lat, zoom].every(Number.isFinite) ? { lon, lat, zoom } : null;
  })();
  const browsing = Boolean(atParam) && !stopId;

  // ?pin=lon,lat — a dropped pin: the residential address the user searched.
  // Drawn as an amber marker once the map is up; the cross-region hand-over
  // treats it like an anchored stop (the pin's region was chosen by
  // nearest-stop, so browsing around it must not get second-guessed).
  const pinRaw = new URLSearchParams(location.search).get("pin");
  const pinParam = (() => {
    if (!pinRaw) return null;
    const [lon, lat] = pinRaw.split(",").map(Number);
    return [lon, lat].every(Number.isFinite) ? { lon, lat } : null;
  })();
  // ?to=stop_id — journey-planner mode (PLANNER.md phase 1): the board
  // becomes itineraries from the viewed stop to this one. Same region only.
  // A POINT destination (?tolat/?tolon — a business, an address) keeps its
  // identity: the journey ends at the door, via a trailing walk from the
  // last stop, never silently "translated" into the nearest stop.
  let toId = new URLSearchParams(location.search).get("to");
  let toName = new URLSearchParams(location.search).get("toname") || "";
  let toPoint = (() => {
    const p = new URLSearchParams(location.search);
    const lat = Number(p.get("tolat")), lon = Number(p.get("tolon"));
    return (p.get("tolat") && Number.isFinite(lat) && Number.isFinite(lon))
      ? { lat, lon } : null;
  })();
  const hasDest = () => Boolean(toId || toPoint);
  const destKey = () => toId
    || (toPoint ? `pt:${toPoint.lat.toFixed(5)},${toPoint.lon.toFixed(5)}` : null);

  // The pin's display label ("397 Christine Avenue, Varsity Lakes QLD" /
  // "Near me") — shown in the titlebar like a stop name.
  const pinLabel = new URLSearchParams(location.search).get("pinlabel") || "";
  // Pinned-browse: the map is anchored to an address rather than a stop.
  // The chrome behaves like a stop view (titlebar + goose, folded search,
  // "Change address") until an actual stop is picked. A pin WITHOUT a stop
  // is a pinned browse even with no ?at= — shared planner URLs carry only
  // pin/to, and the camera falls back to the pin.
  const pinnedBrowse = Boolean(pinParam) && !stopId;
  // The stops surrounding a dropped pin — fetched once the map is up, drawn
  // as clickable landmarks at ANY zoom (the all-stops layer needs z15+, and
  // a sparse suburb's nearest stops can force the camera further out).
  let pinStops = [];

  // --- region ------------------------------------------------------------
  // One board, many networks. The region picks the timetable DB, the realtime
  // feeds, the timezone and the basemap — all server-side; the client just
  // scopes its API calls and reloads when switched (the map style, caches and
  // colour state are all per-network, so a clean start is correct).
  const region = new URLSearchParams(location.search).get("region")
              || localStorage.getItem("region") || "seq";
  localStorage.setItem("region", region);
  const api = (path) => `/api/r/${region}${path}`;

  // Crossing into another region (a search pick, or panning the map there)
  // reloads: the map style, caches and colour state are all per-network, so
  // a clean start is correct. `at=lon,lat,zoom` carries the camera across so
  // the map reopens exactly where the user was looking.
  function switchRegion(next, at) {
    if (next === region) return;
    localStorage.setItem("region", next);
    location.href = `?region=${encodeURIComponent(next)}`
                  + (at ? `&at=${at}` : "");                 // drop the stop
  }

  // Times on the board are the *network's* clock, not the viewer's: a Brisbane
  // browser looking at Melbourne shows AEDT. Filled from /config; until then
  // the viewer's zone is a harmless first-paint fallback.
  let regionTz;
  fetch(api("/config")).then((r) => r.json())
    .then((c) => {
      regionTz = c.tz;
      // Name the clock those times run on: "AWST", "ACST", … — visible
      // whenever the titlebar is (i.e. whenever any time is on screen).
      try {
        const abbr = new Intl.DateTimeFormat("en-AU",
            {timeZone: regionTz, timeZoneName: "short"})
          .formatToParts(new Date())
          .find((p) => p.type === "timeZoneName").value;
        const badge = $("tz-badge");
        $("tz-abbr").textContent = abbr;
        badge.hidden = false;
        badge.title = `All times are ${regionTz} (${abbr})`;
      } catch { /* unknown zone: badge stays empty and hidden */ }
    }).catch(() => {});

  // There is no visible region UI: the ingested-regions list drives the
  // search fan-out (results can come from ANY region, each row carrying its
  // state) and the map's pan-across-regions check (each entry's center).
  let regionList = [{ id: region, name: "", state: "" }];
  const stateOf = (rid) =>
    (regionList.find((r) => r.id === rid) || {}).state || "";
  // Search order: the remembered region's results list first.
  const regionOrder = () =>
    [...regionList].sort((a, b) => (b.id === region) - (a.id === region));
  const regionsReady = fetch("/api/regions").then((r) => r.json())
    .then((regions) => { regionList = regions; })
    .catch(() => { /* search stays single-region, no pan-swap */ });

  function syncChrome() {
    // No stop yet = the landing: just the goose and the search. The class
    // hides the chrome and parks the board/map split off-screen (still sized,
    // so the map keeps warming underneath). Map-browsing mode (?at=, from a
    // pan across regions) keeps the full layout instead.
    const anchored = Boolean(stopId) || pinnedBrowse;
    document.body.classList.toggle("landing", !stopId && !browsing && !pinnedBrowse);
    $("titlebar").hidden = !anchored;
    // Anchored, the search only comes back for a destination pick ("Plan a
    // trip") — changing the ORIGIN means going home and starting over.
    $("search-wrap").hidden = anchored && !pickingDest;
    $("search-close").hidden = !anchored;  // nothing to return to without one
    // A selected stop needs no "change" button — the goose is the way back.
    // A pinned browse gets the goose ON the button instead: one control,
    // home to the landing. Never two geese at once.
    $("change-stop").hidden = Boolean(stopId) || !pinnedBrowse;
    $("home-goose").hidden = !$("change-stop").hidden;
    // Planner entry/exit: available from a stop OR a pinned address (the
    // trip then STARTS at the address — first leg is the walk to a stop).
    // With a destination set, the same button cancels back.
    $("plan-to").hidden = !stopId && !pinnedBrowse;
    $("plan-to").textContent = hasDest() ? `✕ to ${toName || "destination"}`
                                    : "Plan a trip";
    if ($("search-wrap").hidden) $("results").hidden = true;
  }
  if (pinnedBrowse) {
    // The address plays the part of the stop name until a stop is picked.
    $("stop-name").textContent = pinLabel || "Dropped pin";
  }

  // Only reached with pickingDest set (the "Plan a trip" button): anchored,
  // the search exists solely to pick a destination.
  function openSearch() {
    syncChrome();
    $("search").focus();
  }

  function closeSearch() {
    pickingDest = false;
    $("search").value = "";
    $("search").placeholder = "Search for a stop or an address";
    syncChrome();
  }

  // "Change address" flies home to search afresh — carrying whatever was
  // last actually TYPED (never a picked autocompletion), so a mistyped
  // address that landed somewhere dumb is one edit away from fixed.
  $("change-stop").addEventListener("click", () => {
    const q = localStorage.getItem("lastTyped") || "";
    location.href = q ? `/?q=${encodeURIComponent(q)}` : "/";
  });
  $("search-close").addEventListener("click", closeSearch);
