// resize handling and page boot
// Split from index.html; these files load in order as classic
// scripts and share one global scope — boot.js must stay last.
  // Re-fit the board when the viewport changes; the row count is derived from
  // available height, so a resize or an orientation change alters it.
  let resizeDebounce;
  window.addEventListener("resize", () => {
    clearTimeout(resizeDebounce);
    resizeDebounce = setTimeout(() => {
      fitResults();
      if (!lastData) return;
      renderBoard(lastData);
      updateMap(lastData);
    }, 150);
  });

  syncChrome();
  nameUnnamedPoints();   // bare coordinates in the URL get an address
  initMap();
  if (stopId) selectStop(stopId);
  else if (hasDest() && pinnedBrowse) {
    // A reloaded/shared address-origin plan: start polling it directly.
    refresh();
    timer = setInterval(refresh, 15000);
  }
  // ?q= deep-links a search: prefill the box and run it as if typed.
  // Focused with the caret at the end — the "Change address" round-trip
  // lands here precisely so the user can fix what they typed.
  const preQ = new URLSearchParams(location.search).get("q");
  if (preQ && !stopId) {
    const inp = $("search");
    inp.value = preQ;
    inp.dispatchEvent(new Event("input", { bubbles: true }));
    inp.focus();
  }
