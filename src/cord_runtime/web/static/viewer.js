/* Progressive enhancement: URLs/forms/details work without JavaScript. */
window.addEventListener('load', () => {
  const alert = document.querySelector("#connection-alert");
  const status = document.querySelector("#live-status");
  let busy = false;
  let source;
  let lastEvent = 0;
  async function refresh() {
    if (busy || document.hidden) return;
    busy = true;
    try {
      const response = await fetch(location.href, {cache: "no-store"});
      if (!response.ok) throw new Error("unavailable");
      const page = new DOMParser().parseFromString(await response.text(), "text/html");
      const current = document.querySelector("#view-content");
      const next = page.querySelector("#view-content");
      // Never replace an active filter or keyboard target. Preserve disclosure state.
      if (current.innerHTML !== next.innerHTML && !current.contains(document.activeElement)) {
        const open = Array.from(current.querySelectorAll("details")).map(d => d.open);
        current.replaceChildren(...next.childNodes);
        current.querySelectorAll("details").forEach((d, i) => {if (i < open.length) d.open = open[i];});
      }
      const nav = document.querySelector("nav");
      if (!nav.contains(document.activeElement)) nav.replaceChildren(...page.querySelector("nav").childNodes);
      alert.textContent = page.querySelector("#connection-alert").textContent;
      status.textContent = "Observation connected";
    } catch (_) {
      alert.textContent = "Disconnected — showing the last observation. Reconnecting…";
      status.textContent = "Observation disconnected";
    } finally { busy = false; }
  }
  const events = document.querySelector("[data-events]")?.dataset.events;
  if (events && window.EventSource) {
    source = new EventSource(events);
    source.addEventListener("snapshot", event => {
      const id = Number(event.lastEventId);
      if (id && id <= lastEvent) return;
      lastEvent = id;
      const update = JSON.parse(event.data);
      status.textContent = "Source: " + update.source;
      alert.textContent = update.diagnostics.join(". ");
      if (update.source === "ingestion_failed") alert.textContent = "Ingestion failed — no durable record received.";
      refresh();
    });
    source.onerror = () => {alert.textContent = "Live stream disconnected — polling for the latest observation.";};
  }
  // Also discovers new recorded Runs and new connections. Fallback for SSE loss.
  const timer = setInterval(refresh, 3000);
  window.addEventListener("pagehide", () => {clearInterval(timer); source?.close();});
}, {once: true});
