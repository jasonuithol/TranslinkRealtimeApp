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
