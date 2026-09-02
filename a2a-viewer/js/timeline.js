import { renderEvent } from "./renderers/index.js";

export function renderTimeline(trace) {
  const root = document.createElement("div");
  const h2 = document.createElement("h2");
  h2.textContent = `Events (${(trace.events ?? []).length})`;
  root.appendChild(h2);

  for (const ev of trace.events ?? []) {
    root.appendChild(renderEvent(ev));
  }
  return root;
}
