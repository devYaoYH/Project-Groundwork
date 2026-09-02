// Bootstrap: file picker, drag-and-drop, render orchestration.
import "./renderers/index.js";       // registers default
import "./renderers/message.js";     // chat-bubble
import "./renderers/game_lifecycle.js"; // game_start + game_end
// Downstream games can add custom renderers by importing them here, e.g.:
//   import "./renderers/word_guess.js";

import { renderHeader } from "./header.js";
import { renderTimeline } from "./timeline.js";

const fileInput = document.getElementById("file-input");
const headerPanel = document.getElementById("header-panel");
const derivedPanel = document.getElementById("derived-panel");
const observabilityPanel = document.getElementById("observability-panel");
const timelinePanel = document.getElementById("timeline");
const status = document.getElementById("status");
const dropzone = document.getElementById("dropzone");

function setStatus(msg, isError = false) {
  status.textContent = msg;
  status.classList.toggle("error", isError);
}

function render(trace) {
  headerPanel.replaceChildren(renderHeader(trace));
  timelinePanel.replaceChildren(renderTimeline(trace));
}

function renderDerivedMetrics(payload) {
  derivedPanel.replaceChildren();
  const heading = document.createElement("h2");
  heading.textContent = "Derived metrics";
  derivedPanel.appendChild(heading);
  const artifacts = payload?.artifacts ?? [];
  if (!artifacts.length) {
    const note = document.createElement("p");
    note.className = "observability-note";
    note.textContent = "No post-episode metric artifacts have been materialized for this trace.";
    derivedPanel.appendChild(note);
    return;
  }
  for (const artifact of artifacts) {
    const details = document.createElement("details");
    details.className = "otel-span";
    const summary = document.createElement("summary");
    summary.textContent = `${artifact.kind}@${artifact.version}`;
    details.appendChild(summary);
    const pre = document.createElement("pre");
    pre.textContent = JSON.stringify(artifact.payload, null, 2);
    details.appendChild(pre);
    derivedPanel.appendChild(details);
  }
}

function renderObservability(payload, note = null) {
  observabilityPanel.replaceChildren();
  const heading = document.createElement("h2");
  heading.textContent = "OpenTelemetry";
  observabilityPanel.appendChild(heading);
  if (note) {
    const text = document.createElement("p");
    text.className = "observability-note";
    text.textContent = note;
    observabilityPanel.appendChild(text);
    return;
  }
  const spans = payload?.spans ?? [];
  const text = document.createElement("p");
  text.className = "observability-note";
  text.textContent = `${spans.length} local span${spans.length === 1 ? "" : "s"} correlated by trace ID.`;
  observabilityPanel.appendChild(text);
  for (const span of spans) {
    const details = document.createElement("details");
    details.className = "otel-span";
    const summary = document.createElement("summary");
    const status = span.status?.status_code ?? "UNSET";
    summary.textContent = `${span.start_time ?? ""} · ${span.name ?? "span"} · ${status}`;
    details.appendChild(summary);
    const pre = document.createElement("pre");
    pre.textContent = JSON.stringify(span, null, 2);
    details.appendChild(pre);
    observabilityPanel.appendChild(details);
  }
}

async function loadFile(file) {
  try {
    const text = await file.text();
    const trace = JSON.parse(text);
    render(trace);
    setStatus(`Loaded ${file.name} (${(trace.events ?? []).length} events)`);
  } catch (err) {
    setStatus(`Failed to load ${file.name}: ${err.message}`, true);
  }
}

async function loadTraceId(gameId) {
  try {
    const response = await fetch(`/api/traces/${encodeURIComponent(gameId)}`);
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const trace = await response.json();
    render(trace);
    const artifacts = await fetch(`/api/traces/${encodeURIComponent(gameId)}/artifacts`);
    renderDerivedMetrics(artifacts.ok ? await artifacts.json() : null);
    const observability = await fetch(`/api/traces/${encodeURIComponent(gameId)}/observability`);
    if (observability.ok) {
      renderObservability(await observability.json());
    } else {
      renderObservability(null, "No local OTel projection is available for this trace.");
    }
    setStatus(`Loaded ${trace.game_id} (${(trace.events ?? []).length} events)`);
  } catch (err) {
    renderObservability(null, "OTel spans are available when this trace is opened from the local stack.");
    setStatus(`Failed to load trace: ${err.message}`, true);
  }
}

fileInput.addEventListener("change", () => {
  const f = fileInput.files?.[0];
  if (f) loadFile(f);
});

// Drag-and-drop on the whole window.
let dragDepth = 0;
window.addEventListener("dragenter", (e) => {
  if (!e.dataTransfer?.types?.includes("Files")) return;
  e.preventDefault();
  dragDepth += 1;
  dropzone.classList.remove("hidden");
});
window.addEventListener("dragover", (e) => {
  if (e.dataTransfer?.types?.includes("Files")) e.preventDefault();
});
window.addEventListener("dragleave", () => {
  dragDepth = Math.max(0, dragDepth - 1);
  if (dragDepth === 0) dropzone.classList.add("hidden");
});
window.addEventListener("drop", (e) => {
  e.preventDefault();
  dragDepth = 0;
  dropzone.classList.add("hidden");
  const f = e.dataTransfer?.files?.[0];
  if (f) loadFile(f);
});

// The hub routes a browser-selected trace through session storage. Compose's
// local control plane routes persisted SQLite traces through ?trace=<game_id>.
const params = new URLSearchParams(window.location.search);
const traceId = params.get("trace");
if (traceId) {
  loadTraceId(traceId);
} else {
  renderObservability(null, "OTel spans are available when this trace is opened from the local stack.");
  renderDerivedMetrics(null);
  const pending = sessionStorage.getItem("a2a_pending_trace");
  if (pending) {
    sessionStorage.removeItem("a2a_pending_trace");
    try {
      const trace = JSON.parse(pending);
      render(trace);
      setStatus(`Loaded trace (${(trace.events ?? []).length} events)`);
    } catch (err) {
      setStatus(`Failed to load routed trace: ${err.message}`, true);
    }
  }
}
