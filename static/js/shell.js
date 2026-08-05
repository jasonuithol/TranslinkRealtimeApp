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
    ovDismissed = stopId || destKey() || "-";
    ovKind = null;
    $("ov").hidden = true;
    soloCard = null;            // the card list is no longer pinned to one
  }
  // renderPlan honours this: a held solution shows THAT journey alone.
  let soloCard = null;

  // Hooks called by the renderers, so they stay ignorant of the shell.
  function shellFreshAsk() { ovDismissed = null; }

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
          renderPlan(); drawPlan();
          openOverlay("plan");
        });
      bar.appendChild(b);
    });
    if (coachPlace) coachPlace();   // the tip steps aside for the journeys
  }

  // --- boot: the goose waddles until the map is up -------------------------
  function shellReady() {
    $("boot").hidden = true;
    $("toolbar").hidden = false;
    wireTargetHold();          // the control exists by now
  }
  function bootSay(msg) {
    const el = $("boot-say");
    if (el) el.textContent = msg;
  }

  // --- toolbar wiring ------------------------------------------------------
  // GREEN goose: tap re-pins you where you are; hold types an address.
  tapOrHold($("tb-home"),
    () => $("near-me").click(),          // the located-point flow, unchanged
    () => {
      pickingDest = false;
      $("search").placeholder = "Where are you?";
      openSearch();
    });

  // RED goose: drag it onto the map (that logic lives with the map), or
  // hold it to type the destination instead. The hold cancels the drag it
  // interrupted — the ghost pin goes back in its box.
  function wireTargetHold() {
    const btn = document.querySelector(".drop-pin");
    if (!btn || btn.dataset.holdWired) return;
    btn.dataset.holdWired = "1";
    let timer = null, from = null;
    const clear = () => { clearTimeout(timer); timer = null; };
    btn.addEventListener("pointerdown", (e) => {
      from = [e.clientX, e.clientY];
      timer = setTimeout(() => {
        timer = null;
        document.querySelectorAll(".pin-ghost").forEach((g) => g.remove());
        try { btn.releasePointerCapture(e.pointerId); } catch { /* gone */ }
        pickingDest = true;
        $("search").placeholder = "Where to?";
        openSearch();
      }, HOLD_MS);
    });
    btn.addEventListener("pointermove", (e) => {
      if (timer && from
          && Math.hypot(e.clientX - from[0], e.clientY - from[1]) > 10) clear();
    });
    btn.addEventListener("pointerup", clear);
    btn.addEventListener("pointercancel", clear);
  }

  $("ov-x").addEventListener("click", closeOverlay);
  // Escape closes whatever is over the map.
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && !$("ov").hidden) closeOverlay();
  });
