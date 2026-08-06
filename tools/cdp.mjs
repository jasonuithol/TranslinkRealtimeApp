// Drive headless Chrome over the DevTools protocol, with no dependencies:
// node 22 has a global WebSocket, which is enough to grant geolocation, put
// the browser somewhere specific, click things and take screenshots.
//
// Used by the throwaway UI checks in this project. Start a browser first:
//
//   podman run -d --name chromedbg --network host --user root \
//     --entrypoint chromium-browser docker.io/zenika/alpine-chrome:latest \
//     --no-sandbox --headless --disable-gpu --remote-debugging-port=9222 \
//     --remote-debugging-address=0.0.0.0 --hide-scrollbars
//
// Two things worth knowing, both learned the hard way:
//   * Every page you open stays open. A run that throws leaves a tab behind
//     polling the app, and enough of those will bury the machine — close
//     pages, and restart the browser between runs.
//   * eval() returns values BY VALUE. Returning a MapLibre map object asks
//     the protocol to serialise the world and it gives up; wrap statements
//     in `(() => { ...; })()` so they return undefined.
const DBG = process.env.DBG || "http://localhost:9222";

export async function open(url, { lat, lon, viewport } = {}) {
  const t = await (await fetch(`${DBG}/json/new?about:blank`, { method: "PUT" })).json();
  const ws = new WebSocket(t.webSocketDebuggerUrl);
  await new Promise((r, j) => { ws.onopen = r; ws.onerror = j; });
  let id = 0;
  const pending = new Map();
  const events = [];
  ws.onmessage = (m) => {
    const msg = JSON.parse(m.data);
    if (msg.id && pending.has(msg.id)) {
      const { res, rej } = pending.get(msg.id);
      pending.delete(msg.id);
      msg.error ? rej(new Error(JSON.stringify(msg.error))) : res(msg.result);
    } else if (msg.method) {
      events.push(msg);
    }
  };
  const send = (method, params = {}) => new Promise((res, rej) => {
    const n = ++id;
    pending.set(n, { res, rej });
    ws.send(JSON.stringify({ id: n, method, params }));
  });

  await send("Page.enable");
  await send("Runtime.enable");
  await send("Log.enable");
  if (viewport) {
    await send("Emulation.setDeviceMetricsOverride",
      { width: viewport[0], height: viewport[1], deviceScaleFactor: 1, mobile: true });
  }
  if (lat != null) {
    await send("Browser.grantPermissions",
               { origin: new URL(url).origin, permissions: ["geolocation"] });
    await send("Emulation.setGeolocationOverride",
               { latitude: lat, longitude: lon, accuracy: 20 });
  }
  await send("Page.navigate", { url });

  const api = {
    events,
    async eval(expr) {
      const r = await send("Runtime.evaluate",
        { expression: expr, awaitPromise: true, returnByValue: true });
      if (r.exceptionDetails) {
        throw new Error(r.exceptionDetails.exception?.description
                        || JSON.stringify(r.exceptionDetails));
      }
      return r.result.value;
    },
    async waitFor(expr, { timeout = 30000, label = expr } = {}) {
      const t0 = Date.now();
      for (;;) {
        let v = null;
        try { v = await api.eval(expr); } catch { /* still loading */ }
        if (v) return v;
        if (Date.now() - t0 > timeout) throw new Error(`timeout: ${label}`);
        await new Promise((r) => setTimeout(r, 200));
      }
    },
    async shot(path) {
      const r = await send("Page.captureScreenshot", { format: "png" });
      const { writeFileSync } = await import("node:fs");
      writeFileSync(path, Buffer.from(r.data, "base64"));
    },
    async close() {
      try { await send("Page.close"); } catch { /* already gone */ }
      ws.close();
    },
    errors() {
      return events
        .filter((e) => e.method === "Log.entryAdded"
                    && e.params.entry.level === "error")
        .map((e) => e.params.entry.text);
    },
  };
  return api;
}
