// the shell: full-screen map, a floating toolbar, overlay cards
// Split from index.html; these files load in order as classic
// scripts and share one global scope — boot.js must stay last.
  // --- shell ---------------------------------------------------------------
  // The workflow reads left to right along the toolbar: the GREEN goose is
  // where you are, the RED goose is where you're going, and each journey
  // the planner finds becomes a button after them. Tap a journey to draw
  // it; hold it to read its card. Nothing on this page scrolls — the map
  // fills the viewport and everything else floats over it.
  //
  // Tap vs hold is the whole grammar: tap acts, hold offers the typed
  // alternative (an address instead of GPS, a search instead of a drag).
  const HOLD_MS = 450;

  // One handler pair for every toolbar button: fires hold OR tap, never
  // both, and cancels cleanly if the finger wanders off (a drag, a scroll
  // attempt, a change of mind).
  function tapOrHold(el, onTap, onHold) {
    let timer = null, held = false, from = null;
    const clear = () => { clearTimeout(timer); timer = null; };
    el.addEventListener("pointerdown", (e) => {
      if (e.button > 0) return;
      held = false;
      from = [e.clientX, e.clientY];
      timer = setTimeout(() => { held = true; timer = null; onHold(); }, HOLD_MS);
    });
    el.addEventListener("pointermove", (e) => {
      if (!timer || !from) return;
      if (Math.hypot(e.clientX - from[0], e.clientY - from[1]) > 10) clear();
    });
    el.addEventListener("pointerup", () => {
      if (held) { held = false; return; }   // the hold already acted
      if (timer) { clear(); onTap(); }
    });
    el.addEventListener("pointercancel", clear);
    el.addEventListener("pointerleave", clear);
    // Keyboard: Enter acts, Shift+Enter offers the typed alternative.
    el.addEventListener("keydown", (e) => {
      if (e.key !== "Enter" && e.key !== " ") return;
      e.preventDefault();
      e.shiftKey ? onHold() : onTap();
    });
  }

  // Drag a goose off its button and onto the map. Both geese answer the
  // same three gestures — tap, hold, drag — so one implementation serves
  // them: the ghost follows the finger, and the drop hands back where it
  // landed. Pointer capture keeps move/up coming to the button wherever
  // the finger goes; touch-action: none stops the page fighting for the
  // gesture. A drop that misses the map is a change of heart, and free.
  function gooseGesture(btn, { kind, onTap, onHold, onDrop }) {
    let ghost = null, from = null, timer = null, held = false, dragging = false;
    const clearTimer = () => { clearTimeout(timer); timer = null; };
    const dropGhost = () => { if (ghost) { ghost.remove(); ghost = null; } };
    btn.addEventListener("contextmenu", (e) => e.preventDefault());
    btn.addEventListener("pointerdown", (e) => {
      if (e.button > 0) return;
      e.preventDefault();
      btn.setPointerCapture(e.pointerId);
      from = [e.clientX, e.clientY];
      held = false; dragging = false;
      if (onHold) timer = setTimeout(() => {
        timer = null; held = true;
        dropGhost();                 // a hold is not a drag
        onHold();
      }, HOLD_MS);
    });
    btn.addEventListener("pointermove", (e) => {
      if (!from || held) return;
      const far = Math.hypot(e.clientX - from[0], e.clientY - from[1]) > 8;
      if (far && !dragging && onDrop) {
        clearTimer();                // moving means dragging, not holding
        dragging = true;
        ghost = document.createElement("div");
        ghost.className = `pin-ghost ${kind}`;
        ghost.innerHTML = '<svg viewBox="440 -1740 1720 2080" width="18" '
          + 'height="20" aria-hidden="true"><use href="#goose-shape"/></svg>';
        document.body.appendChild(ghost);
      } else if (far) {
        clearTimer();
      }
      if (ghost) {
        ghost.style.left = `${e.clientX}px`;
        ghost.style.top = `${e.clientY}px`;
      }
    });
    btn.addEventListener("pointercancel", () => { clearTimer(); dropGhost(); });
    btn.addEventListener("pointerup", (e) => {
      clearTimer();
      dropGhost();
      const moved = from
        && Math.hypot(e.clientX - from[0], e.clientY - from[1]) >= 8;
      from = null;
      if (held) { held = false; return; }
      if (!moved) { if (onTap) onTap(); return; }
      if (!onDrop || !mapReady) return;
      const rect = $("map").getBoundingClientRect();
      const x = e.clientX - rect.left, y = e.clientY - rect.top;
      if (x < 0 || y < 0 || x > rect.width || y > rect.height) return;
      const ll = map.unproject([x, y]);
      onDrop(ll.lat, ll.lng);
    });
    // Keyboard: Enter acts, Shift+Enter offers the typed alternative.
    btn.addEventListener("keydown", (e) => {
      if (e.key !== "Enter" && e.key !== " ") return;
      e.preventDefault();
      (e.shiftKey && onHold) ? onHold() : (onTap && onTap());
    });
  }

  // --- the overlay: arrivals or a journey card, never both -----------------
  let ovKind = null;            // "board" | "plan" | null
  function openOverlay(kind) {
    ovKind = kind;
    ovDismissed = null;
    $("ov").hidden = false;
    $("ov").scrollTop = 0;
  }
  // What the user last dismissed. The board re-renders every 15 s poll; a
  // closed overlay must not spring back open on the next tick.
  let ovDismissed = null;
  function closeOverlay() {
    const wasBoard = ovKind === "board";
    ovDismissed = stopId || destKey() || "-";
    ovKind = null;
    $("ov").hidden = true;
    soloCard = null;            // the card list is no longer pinned to one
    // Dismissing arrivals dismisses the STOP: its marker and its buses
    // have no meaning once the card explaining them is gone.
    if (wasBoard) deselectStop();
  }
  // renderPlan honours this: a held solution shows THAT journey alone.
  let soloCard = null;

  // Hooks called by the renderers, so they stay ignorant of the shell.
  function shellFreshAsk() { ovDismissed = null; ovKind = "board"; }

  // Both renderers write to #board, and both can be live at once now —
  // journeys on the toolbar while a stop's arrivals fill the overlay.
  // Whoever the overlay is showing gets the real element; the other
  // renders into a throwaway so its work is kept without stealing the
  // screen. (Its toolbar chips and map drawing happen either way.)
  function boardTargetFor(kind) {
    return (ovKind === null || ovKind === kind)
      ? $("board") : document.createElement("div");
  }

  // Something went wrong and the board is holding the message: show it.
  function shellShowError() { openOverlay("board"); }

  function shellShowBoard() {
    // Arrivals rendered: show them, unless a journey card owns the overlay
    // or the user has closed this stop's board already.
    if (ovKind === "plan") return;
    if (ovDismissed === (stopId || "-")) return;
    openOverlay("board");
  }
  function shellSyncSolutions() {
    const bar = $("tb-solutions");
    if (!bar) return;
    const its = (planData && planData.__shown) || [];
    bar.innerHTML = "";
    // The first plan for a region builds its timetable — seconds, not
    // milliseconds. The old board said "Planning…" but the board lives in
    // a hidden overlay now, so a dropped goose looked like it did nothing.
    if (!its.length && hasDest()) {
      const wait = document.createElement("span");
      wait.className = "tb tb-wait";
      wait.innerHTML = '<span class="tb-wait-goose">'
        + '<svg viewBox="440 -1740 1720 2080" aria-hidden="true">'
        + '<use href="#goose-shape"/></svg></span>';
      wait.title = "Planning your journey…";
      wait.setAttribute("aria-label", wait.title);
      bar.appendChild(wait);
      if (coachPlace) coachPlace();
      return;
    }
    its.forEach((it, i) => {
      const b = document.createElement("button");
      b.type = "button";
      b.className = "tb tb-sol" + (i === selItin ? " on" : "");
      const first = it.legs[0];
      const colour = (planData.__colors || {})[
        first.kind === "ride" ? `${i}:${first.trip_id}` : `walk:${i}:0`];
      if (colour) b.style.setProperty("--vcolor", colour);
      b.innerHTML = `<span class="sol-n">${i + 1}</span>`
                  + `<span class="sol-t">${planClock(it.depart)}</span>`;
      b.title = `Journey ${i + 1}, ${planClock(it.depart)} — hold for details`;
      b.setAttribute("aria-label", b.title);
      tapOrHold(b,
        () => {                        // tap: draw it on the map
          selItin = i; planFitted = false;
          closeOverlay();
          renderPlan(); drawPlan();
        },
        () => {                        // hold: read its card
          selItin = i; planFitted = false;
          soloCard = i;
          // Claim the overlay BEFORE rendering: boardTargetFor sends the
          // card to a throwaway while the overlay still says "arrivals".
          openOverlay("plan");
          renderPlan(); drawPlan();
        });
      bar.appendChild(b);
    });
    if (coachPlace) coachPlace();   // the tip steps aside for the journeys
  }

  // --- boot: the goose waddles until there is somewhere to stand ----------
  // A cold start has no context, so the first step of the workflow runs
  // itself: find the rider. The goose keeps walking through the GPS query
  // AND the map load — standing it down when the map alone was ready just
  // dumped a search box on someone who had asked for nothing.
  let locating = false;
  function shellReady() {
    $("toolbar").hidden = false;
    wireTargetHold();          // the control exists by now
    if (!locating) $("boot").hidden = true;
  }
  // Give up on GPS: the goose steps aside and the search takes over.
  function bootToSearch(why) {
    locating = false;
    $("boot").hidden = true;
    bootSay("Finding you…");
    pickingDest = false;
    pickingOrigin = true;
    $("search").placeholder = "Where are you?";
    openSearch();
    if (why) {
      $("results").innerHTML = "";
      resultNote(why);
      $("results").hidden = false;
      fitResults();
    }
  }
  function shellBootFlow() {
    // Any context in the URL — a stop, a pin, a destination — means the
    // rider already said where they are. Nothing to find.
    if (stopId || pinParam || hasDest()) return;
    if (!("geolocation" in navigator) || !window.isSecureContext) {
      bootToSearch(null);      // http, or no GPS: straight to the search
      return;
    }
    locating = true;
    bootSay("Finding you…");
    let done = false;
    // A phone that never answers must not strand the goose forever.
    const giveUp = setTimeout(() => {
      if (done) return;
      done = true;
      bootToSearch("Still looking for you — search an address instead.");
    }, 15000);
    navigator.geolocation.getCurrentPosition(
      (pos) => {
        if (done) return;
        done = true; clearTimeout(giveUp);
        bootSay("Finding your stops…");
        // openPinned reloads onto ?pin=… — the goose walks until it does.
        openPinned(pos.coords.latitude, pos.coords.longitude, "you", "");
      },
      (err) => {
        if (done) return;
        done = true; clearTimeout(giveUp);
        bootToSearch(err && err.code === 1
          ? "Location declined — search an address instead."
          : "Could not get your location — search an address instead.");
      },
      { enableHighAccuracy: false, timeout: 12000, maximumAge: 120000 });
  }
  function bootSay(msg) {
    const el = $("boot-say");
    if (el) el.textContent = msg;
  }

  // --- toolbar wiring ------------------------------------------------------
  // GREEN goose — where you are. Tap: find me by GPS. Hold: type an
  // address. Drag: put me exactly there, no GPS and no typing.
  gooseGesture($("tb-home"), {
    kind: "depart",
    onTap: () => $("near-me").click(),   // the located-point flow, unchanged
    onHold: () => {
      pickingDest = false;
      pickingOrigin = true;      // the search stays up even when anchored
      $("search").placeholder = "Where are you?";
      openSearch();
    },
    onDrop: (lat, lon) => {
      if (pinParam) { movePin(lat, lon); return; }
      // No pin yet (a stop-only or cold view): openPinned makes one, and
      // picks the region from the nearest stop.
      openPinned(lat, lon, "there", "");
    },
  });

  // RED goose: drag it onto the map (that logic lives with the map), or
  // hold it to type the destination instead. The hold cancels the drag it
  // interrupted — the ghost pin goes back in its box.
  // RED goose — where you're going. The same three gestures; a real drop
  // sets the destination and retires the coach mark for good.
  function wireTargetHold() {
    const btn = document.querySelector(".drop-pin");
    if (!btn || btn.dataset.holdWired) return;
    btn.dataset.holdWired = "1";
    const typeIt = () => {
      pickingDest = true;
      $("search").placeholder = "Where to?";
      openSearch();
    };
    gooseGesture(btn, {
      kind: "dest",
      onTap: typeIt,
      onHold: typeIt,
      onDrop: (lat, lon) => {
        try { localStorage.setItem("honkerPinDragged", "1"); }
        catch { /* private mode */ }
        setDestPoint(lat, lon, "dropped pin");
      },
    });
  }

  $("ov-x").addEventListener("click", closeOverlay);
  // Escape closes whatever is over the map.
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && !$("ov").hidden) closeOverlay();
  });
