// settings: what the app is holding, and how to be rid of it
// Split from index.html; these files load in order as classic
// scripts and share one global scope — boot.js must stay last.
  // --- settings -------------------------------------------------------------
  // Three things a rider (or someone testing this) actually needs: see the
  // walkthrough again, see what data is on the device, and get rid of it.
  // Everything here is about state the app has accumulated — nothing that
  // belongs in the workflow has a home in this panel.

  function settingsOpen() {
    // A toolbar tip has nothing to teach across a panel covering the
    // toolbar. New ones are suppressed while this is open; this clears
    // whichever one was mid-sentence.
    if (typeof coachClear === "function") coachClear();
    $("settings").hidden = false;
    settingsRender();
  }
  function settingsClose() { $("settings").hidden = true; }

  function settingsSection(title) {
    const el = document.createElement("section");
    el.className = "set-sec";
    el.innerHTML = `<h3>${escapeHtml(title)}</h3>`;
    return el;
  }

  function settingsButton(label, cls, onClick) {
    const b = document.createElement("button");
    b.type = "button";
    b.className = `set-btn ${cls}`;
    b.textContent = label;
    b.addEventListener("click", onClick);
    return b;
  }

  function settingsNote(text) {
    const p = document.createElement("p");
    p.className = "set-note";
    p.textContent = text;
    return p;
  }

  // Every region this device has downloaded anything for, newest first.
  function settingsInstalled() {
    const out = [];
    for (let i = 0; i < localStorage.length; i++) {
      const key = localStorage.key(i);
      if (!key || !key.startsWith("pack:")) continue;
      const region = key.slice(5);
      const rec = packLocal(region);
      if (!rec || !rec.files) continue;
      const files = Object.entries(rec.files);
      out.push({
        region,
        name: rec.name || region,
        at: rec.at || 0,
        bytes: files.reduce((n, [, f]) => n + (f.bytes || 0), 0),
        files,
      });
    }
    return out.sort((a, b) => b.at - a.at);
  }

  function settingsWhen(ms) {
    if (!ms) return "";
    const days = Math.floor((Date.now() - ms) / 86400000);
    if (days <= 0) return "today";
    if (days === 1) return "yesterday";
    if (days < 30) return `${days} days ago`;
    return new Date(ms).toLocaleDateString();
  }

  function settingsRender() {
    const body = $("settings-body");
    body.innerHTML = "";

    // --- the walkthrough --------------------------------------------------
    const tips = settingsSection("Tips");
    tips.appendChild(settingsNote(
      "The toolbar walkthrough already starts fresh each time the app is "
      + "opened. This brings it back without closing anything."));
    tips.appendChild(settingsButton("Show the tips again", "set-go", () => {
      // coachStep and its live bubble live in shell.js; reset both and let
      // the normal rules decide which tip is next.
      coachStep = 0;
      coachClear();
      settingsClose();
      coachShow();
    }));
    body.appendChild(tips);

    // --- offline data -----------------------------------------------------
    const data = settingsSection("Offline data");
    const installed = settingsInstalled();
    // What is on the device is a fact about the device, listed whether or
    // not new downloads are currently on offer — saying "nothing here"
    // while holding 373 MB is the kind of lie that makes a settings panel
    // worthless.
    if (!installed.length) {
      data.appendChild(settingsNote(packsSupported()
        ? "No region downloaded yet. One is offered when the app finds you, "
          + "or when a goose lands somewhere new."
        : "This browser keeps no offline data — the app is answered by the "
          + "server. The packaged app downloads a region and works without "
          + "a signal."));
    }
    for (const pack of installed) {
      const row = document.createElement("div");
      row.className = "set-pack";
      const files = pack.files
        .map(([name, f]) => `${name.replace(/\.(sqlite3|pmtiles)$/, "")}`
                          + ` ${packSay(f.bytes || 0)}`)
        .join(" · ");
      row.innerHTML =
        `<div class="set-pack-head"><b>${escapeHtml(pack.name)}</b>`
        + `<span>${packSay(pack.bytes)}</span></div>`
        + `<div class="set-pack-files">${escapeHtml(files)}</div>`
        + (pack.at ? `<div class="set-pack-files">downloaded `
                   + `${escapeHtml(settingsWhen(pack.at))}</div>` : "");

      const acts = document.createElement("div");
      acts.className = "set-pack-acts";
      // The timetable is the only part that expires: everything else is
      // geography. So "update" means the timetable, and says so. With no
      // source configured there is nothing to check against, but the
      // pack is still here and still deletable.
      if (packsSupported()) {
        acts.appendChild(settingsButton("Check for a new timetable", "set-alt",
          () => settingsUpdate(pack)));
      }
      acts.appendChild(settingsButton("Delete", "set-danger",
        () => settingsDelete(pack, row)));
      row.appendChild(acts);
      data.appendChild(row);
    }
    body.appendChild(data);

    if (packBaseSet) {
      const src = settingsSection("Pack source");
      src.appendChild(settingsNote(
        `Packs come from ${packBaseSet}, set by a ?packbase= link and `
        + "remembered — the app drops the parameter when it navigates, so "
        + "reading it once would lose it a second later."));
      // Say what forgetting it actually does, which is not the same on both
      // platforms. The packaged app falls back to the published release; a
      // browser has no other source it can use, so packs simply stop being
      // offered — calling that button "use the published packs" was a
      // promise it could not keep.
      src.appendChild(settingsNote(window.Capacitor
        ? "Forgetting it falls back to the published release."
        : "Forgetting it stops packs being offered in this browser until "
          + "you open a ?packbase= link again. Data already downloaded is "
          + "kept."));
      src.appendChild(settingsButton("Forget this pack source", "set-alt", () => {
        localStorage.removeItem("packbase");
        location.reload();
      }));
      body.appendChild(src);
    }
  }

  // Fetch a fresh manifest — past the session's memo, which is the whole
  // point of asking — and download whatever has moved.
  async function settingsUpdate(pack) {
    const body = $("settings-body");
    const say = document.createElement("p");
    say.className = "set-note";
    say.textContent = "Checking…";
    body.prepend(say);
    try {
      delete packManifestCache[pack.region];
      const manifest = await packManifest(pack.region);
      const plan = packPlan(packLocal(pack.region), manifest);
      if (plan.kind === "current") {
        say.textContent = "The timetable is already the current one.";
        return;
      }
      settingsClose();
      await packDownload({ id: pack.region, name: pack.name }, plan);
      settingsOpen();
    } catch (err) {
      say.textContent = `Could not check: ${err.message}`;
    }
  }

  // Deleting is the one thing here that destroys something, so it asks —
  // and says how much comes back down if they change their mind.
  async function settingsDelete(pack, row) {
    if (row.querySelector(".set-confirm")) return;   // already asking
    const confirmRow = document.createElement("div");
    confirmRow.className = "set-confirm";
    confirmRow.innerHTML =
      `<span>Delete ${escapeHtml(pack.name)}? `
      + `${escapeHtml(packSay(pack.bytes))} to download again.</span>`;
    confirmRow.appendChild(settingsButton("Delete", "set-danger", async () => {
      confirmRow.textContent = "Deleting…";
      for (const [name, f] of pack.files) {
        await packStore.remove(pack.region, f.stored || name);
      }
      localStorage.removeItem(`pack:${pack.region}`);
      settingsRender();
    }));
    confirmRow.appendChild(settingsButton("Keep it", "set-alt",
      () => confirmRow.remove()));
    row.appendChild(confirmRow);
  }

  $("tb-menu").addEventListener("click", settingsOpen);
  $("settings-x").addEventListener("click", settingsClose);
  $("settings").addEventListener("click", (e) => {
    if (e.target === $("settings")) settingsClose();   // tap the scrim
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && !$("settings").hidden) settingsClose();
  });
