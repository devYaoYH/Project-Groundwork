const $ = selector => document.querySelector(selector);

let activeStream = null;
let followedRollout = null;
let selectedEnvironment = null;          // filters configs and experiments
const configsByRelease = new Map();      // release id -> checked-in config paths
const releasesById = new Map();
const experimentsById = new Map();
let visibleExperiments = [];

async function api(path, options) {
  const response = await fetch(path, {headers: {"Content-Type": "application/json"}, ...options});
  const payload = await response.json();
  if (!response.ok) throw new Error(payload.error || `HTTP ${response.status}`);
  return payload;
}

function esc(value) {
  return String(value ?? "").replace(/&/g, "&amp;").replace(/</g, "&lt;")
    .replace(/>/g, "&gt;").replace(/"/g, "&quot;");
}

const basename = path => String(path || "").split("/").pop();

// ---------------------------------------------------------------- tabs

function selectTab(id) {
  for (const tab of document.querySelectorAll('[role="tab"]')) {
    const active = tab.id === id;
    tab.setAttribute("aria-selected", String(active));
    document.getElementById(tab.getAttribute("aria-controls")).hidden = !active;
  }
}

document.querySelectorAll('[role="tab"]').forEach(
  tab => tab.addEventListener("click", () => selectTab(tab.id)));

// ---------------------------------------------------------------- details dialog

function showDetails(title, rows, extra = "") {
  $("#details-title").textContent = title;
  $("#details-body").innerHTML =
    `<dl class="kv">${rows.map(([key, value]) =>
      `<dt>${esc(key)}</dt><dd>${value}</dd>`).join("")}</dl>${extra}`;
  $("#details").showModal();
}

$("#details-close").addEventListener("click", () => $("#details").close());

// ---------------------------------------------------------------- environments

function renderEnvironments(releases) {
  $("#environments").innerHTML = releases.map(release => {
    const count = (configsByRelease.get(release.id) || []).length;
    const selected = selectedEnvironment === release.id;
    return `<div class="env" role="button" tabindex="0" data-env="${esc(release.id)}"
      aria-pressed="${selected}">
      <span><span class="name">${esc(release.game_name)}</span><br>
      <code>${esc(release.package || "unpackaged")}</code></span>
      <span class="count">${count} config${count === 1 ? "" : "s"}</span>
    </div>`;
  }).join("") || "No installed environments.";

  document.querySelectorAll("[data-env]").forEach(node => {
    const toggle = () => {
      // Clicking the selected environment clears the filter rather than
      // stranding the reader with no way back to everything.
      selectedEnvironment = selectedEnvironment === node.dataset.env ? null : node.dataset.env;
      refresh().catch(error => { $("#message").textContent = error.message; });
    };
    node.addEventListener("click", toggle);
    node.addEventListener("keydown", event => {
      if (event.key === "Enter" || event.key === " ") { event.preventDefault(); toggle(); }
    });
  });
}

function renderFilterNote(element, total, shown) {
  if (!selectedEnvironment) { element.hidden = true; return; }
  const game = releasesById.get(selectedEnvironment)?.game_name || selectedEnvironment;
  element.hidden = false;
  element.innerHTML =
    `<span class="muted">Showing ${shown} of ${total} for <strong>${esc(game)}</strong>.</span>
     <button class="secondary" data-clear-filter>Show all</button>`;
  element.querySelector("[data-clear-filter]").onclick = () => {
    selectedEnvironment = null;
    refresh().catch(error => { $("#message").textContent = error.message; });
  };
}

// ---------------------------------------------------------------- agent pool

// Read-only on purpose: the pool is a file a researcher owns, and the browser
// selects rather than writes configuration.
async function renderAgentPool() {
  const target = $("#agent-pool");
  try {
    const payload = await api("/api/agent-pool");
    if (!payload.agents.length) {
      target.textContent = "No agent pool found.";
      return;
    }
    const rows = payload.agents.map(agent => {
      const credential = agent.credential
        ? `<code>${esc(agent.credential)}</code> ${agent.credential_present
            ? `<span class="ready yes">set</span>` : `<span class="ready no">missing</span>`}`
        : `<span class="muted">none needed</span>`;
      return `<tr><td><code>${esc(agent.name)}</code></td>
        <td>${esc(agent.model ?? "—")}</td><td>${esc(agent.api_format ?? agent.type)}</td>
        <td>${credential}</td>
        <td class="muted">${esc(agent.description || "")}</td></tr>`;
    }).join("");
    target.innerHTML = `<div class="table-scroll"><table class="agents"><thead><tr>
      <th>Name</th><th>Model</th><th>Format</th><th>Credential</th><th>Notes</th>
      </tr></thead><tbody>${rows}</tbody></table></div>`;
  } catch (error) {
    target.textContent = error.message;
  }
}

// ---------------------------------------------------------------- configs

function renderConfigs() {
  const paths = configsByRelease.get($("#release").value) || [];
  const select = $("#yaml-path");
  select.innerHTML = paths.length
    ? paths.map(path => `<option value="${esc(path)}">${esc(basename(path))}</option>`).join("")
    : `<option value="">no experiment configurations found</option>`;
  select.disabled = paths.length === 0;
  renderConfigPreview();
}

const configCache = new Map();

async function loadConfig(path) {
  if (!configCache.has(path)) {
    configCache.set(path, api(`/api/experiment-config?path=${encodeURIComponent(path)}`));
  }
  return configCache.get(path);
}

function readinessBadge(payload) {
  if (payload.ready_for_live === true) return `<span class="ready yes">Ready for live</span>`;
  if (payload.ready_for_live === false) {
    return `<span class="ready no">Missing ${esc(payload.missing_credentials.join(", "))}</span>`;
  }
  return `<span class="ready unknown">Credentials unknown</span>`;
}

function renderAgentSummary(payload) {
  const target = $("#agent-summary");
  if (!payload.agents.length) {
    target.innerHTML = `${readinessBadge(payload)}<p class="muted" style="margin:6px 0 0">${
      esc(payload.readiness_note || "This configuration declares no agents.")}</p>`;
    return;
  }
  const rows = payload.agents.map(agent => {
    const credential = agent.credential_env_var
      ? `<code>${esc(agent.credential_env_var)}</code> ${agent.credential_present
          ? `<span class="ready yes">set</span>` : `<span class="ready no">missing</span>`}`
      : `<span class="muted">${agent.model ? "no env key" : "no model"}</span>`;
    return `<tr><td>${esc(agent.batch_label)}</td><td>${esc(agent.index)}</td>
      <td>${esc(agent.type)}</td><td>${esc(agent.model ?? "—")}</td>
      <td>${esc(agent.provider ?? "—")}</td><td>${credential}</td></tr>`;
  }).join("");
  target.innerHTML = `${readinessBadge(payload)}
    <table class="agents"><thead><tr>
      <th>Batch</th><th>#</th><th>Type</th><th>Model</th><th>Provider</th><th>Credential</th>
    </tr></thead><tbody>${rows}</tbody></table>`;
}

async function renderConfigPreview() {
  const path = $("#yaml-path").value;
  const preview = $("#config-preview");
  const summary = $("#agent-summary");
  if (!path) {
    preview.textContent = "Select a configuration to read it.";
    summary.textContent = "Select a configuration.";
    return;
  }
  preview.textContent = `Loading ${basename(path)}…`;
  summary.textContent = "Loading…";
  try {
    const payload = await loadConfig(path);
    preview.textContent = `# ${payload.yaml_path}\n# sha256 ${payload.sha256}\n\n${payload.content}`;
    renderAgentSummary(payload);
  } catch (error) {
    preview.textContent = error.message;
    summary.textContent = error.message;
  }
}

// Live mode is the only mode that needs credentials, so the warning appears
// with the choice that needs it rather than after the run has failed.
async function renderLaunchReadiness(experiments) {
  const target = $("#launch-readiness");
  if (selectedRunMode() !== "live" || !experiments.length) { target.innerHTML = ""; return; }
  const checks = await Promise.all(experiments.map(async experiment => {
    try { return [experiment, await loadConfig(experiment.yaml_path)]; }
    catch { return [experiment, null]; }
  }));
  const blocked = checks.filter(([, payload]) => payload && payload.ready_for_live === false);
  const unknown = checks.filter(([, payload]) => payload && payload.ready_for_live === null);
  if (blocked.length) {
    const missing = [...new Set(blocked.flatMap(([, p]) => p.missing_credentials))];
    target.innerHTML = `<p class="banner no"><strong>${blocked.length}</strong> of these will fail live:
      ${esc(missing.join(", "))} not set in the runner environment.</p>`;
  } else if (unknown.length) {
    target.innerHTML = `<p class="banner unknown">${unknown.length} configuration${
      unknown.length === 1 ? " declares" : "s declare"} no agents, so their models cannot be
      checked before launch.</p>`;
  } else {
    target.innerHTML = "";
  }
}

// ---------------------------------------------------------------- refresh

async function refresh() {
  const [releasePayload, experimentPayload, rolloutPayload] = await Promise.all([
    api("/api/releases"), api("/api/experiments"), api("/api/rollouts"),
  ]);

  const releases = releasePayload.releases;
  configsByRelease.clear();
  releasesById.clear();
  releases.forEach(release => {
    // Re-read on every refresh: a config added to the workspace appears here
    // without restarting the stack.
    configsByRelease.set(release.id, release.experiments || []);
    releasesById.set(release.id, release);
  });
  if (selectedEnvironment && !releasesById.has(selectedEnvironment)) selectedEnvironment = null;
  renderEnvironments(releases);

  const previous = $("#release").value;
  $("#release").innerHTML = releases
    .map(release => `<option value="${esc(release.id)}">${esc(release.game_name)} · ${esc(release.source_ref)}</option>`)
    .join("");
  const wanted = selectedEnvironment || previous;
  if (wanted && configsByRelease.has(wanted)) $("#release").value = wanted;
  renderConfigs();

  const experiments = experimentPayload.experiments;
  experimentsById.clear();
  experiments.forEach(experiment => experimentsById.set(experiment.id, experiment));
  const visible = selectedEnvironment
    ? experiments.filter(experiment => experiment.release_id === selectedEnvironment)
    : experiments;

  renderFilterNote($("#launch-filter"), experiments.length, visible.length);

  $("#launchable").innerHTML = visible.map(experiment =>
    `<div class="row"><strong>${esc(experiment.name)}</strong> · ${esc(experiment.game_name)}<br>
     <code>${esc(basename(experiment.yaml_path))}</code><br>
     <button data-launch="${esc(experiment.id)}" data-name="${esc(experiment.name)}">Launch smoke rollout</button></div>`
  ).join("") || (selectedEnvironment ? "No experiments for this environment." : "Register an experiment first.");

  $("#rollouts").innerHTML = rolloutPayload.rollouts.map(rollout => {
    const experiment = experimentsById.get(rollout.experiment_id);
    const label = experiment
      ? `<strong>${esc(experiment.name)}</strong> · ${esc(experiment.game_name)}`
      : `<span class="muted">experiment removed</span>`;
    // Cancelling only means something while work is outstanding.
    const cancellable = rollout.status === "QUEUED" || rollout.status === "RUNNING";
    return `<div class="row">
      <span class="status ${esc(rollout.status)}">${esc(rollout.status)}</span> ${label}<br>
      <code>${esc(experiment ? basename(experiment.yaml_path) : rollout.experiment_id)}</code><br>
      <button class="secondary" data-rollout="${esc(rollout.id)}">Details</button>
      <button class="secondary" data-follow="${esc(rollout.id)}">Episodes</button>
      ${cancellable ? `<button class="secondary" data-cancel="${esc(rollout.id)}">Cancel</button>` : ""}
    </div>`;
  }).join("") || "No rollouts yet.";

  document.querySelectorAll("[data-launch]").forEach(button =>
    button.onclick = () => launch(button.dataset.launch, button.dataset.name));
  document.querySelectorAll("[data-follow]").forEach(button =>
    button.onclick = () => follow(button.dataset.follow));
  document.querySelectorAll("[data-cancel]").forEach(button =>
    button.onclick = () => cancel(button.dataset.cancel));
  document.querySelectorAll("[data-experiment]").forEach(button =>
    button.onclick = () => showExperimentDetails(button.dataset.experiment));
  document.querySelectorAll("[data-rollout]").forEach(button =>
    button.onclick = () => showRolloutDetails(button.dataset.rollout));
  renderRunModeNote();
  visibleExperiments = visible;
  renderLaunchReadiness(visible).catch(() => {});

  // An episode list already on screen must not keep showing a finished
  // rollout's earlier state.
  if (!followedRollout && rolloutPayload.rollouts.length) {
    followedRollout = rolloutPayload.rollouts[0].id;
  }
  if (followedRollout) renderAttempts(followedRollout).catch(() => {});

  schedulePoll(rolloutPayload.rollouts);
}

// Rollout state advances in the runner, not the browser. Without a poll the
// list only moves when something else happens to trigger a redraw, so a
// finished rollout can sit there reading RUNNING indefinitely.
const POLL_INTERVAL_MS = 3000;
let pollTimer = null;

function schedulePoll(rollouts) {
  const outstanding = rollouts.some(
    rollout => rollout.status === "QUEUED" || rollout.status === "RUNNING"
      || rollout.status === "CANCELLING");
  clearTimeout(pollTimer);
  pollTimer = null;
  if (outstanding) {
    pollTimer = setTimeout(() => refresh().catch(() => {}), POLL_INTERVAL_MS);
  }
}

// ---------------------------------------------------------------- details

async function showExperimentDetails(experimentId) {
  const experiment = experimentsById.get(experimentId);
  if (!experiment) return;
  showDetails(experiment.name, [
    ["Game", esc(experiment.game_name)],
    ["Environment", `<code>${esc(experiment.release_id)}</code>`],
    ["Configuration", `<code>${esc(experiment.yaml_path)}</code>`],
    ["Config sha256", `<code>${esc(experiment.config_sha256)}</code>`],
    ["Registered", esc(experiment.created_at)],
  ], `<pre id="details-config">Loading configuration…</pre>`);

  try {
    const payload = await api(`/api/experiment-config?path=${encodeURIComponent(experiment.yaml_path)}`);
    $("#details-config").textContent = payload.content;
  } catch (error) {
    $("#details-config").textContent = error.message;
  }
}

async function showRolloutDetails(rolloutId) {
  const detail = await api(`/api/rollouts/${encodeURIComponent(rolloutId)}`);
  const rollout = detail.rollout;
  const experiment = experimentsById.get(rollout.experiment_id);
  const counts = detail.episode_attempts.reduce((totals, attempt) => {
    totals[attempt.status] = (totals[attempt.status] || 0) + 1;
    return totals;
  }, {});

  showDetails(experiment ? experiment.name : "Rollout", [
    ["Status", `<span class="status ${esc(rollout.status)}">${esc(rollout.status)}</span>`],
    ["Game", esc(experiment?.game_name ?? "—")],
    ["Configuration", `<code>${esc(experiment?.yaml_path ?? "—")}</code>`],
    ["Rollout id", `<code>${esc(rollout.id)}</code>`],
    ["Episodes", Object.entries(counts).map(([status, count]) =>
      `<span class="status ${esc(status)}">${esc(status)} ${count}</span>`).join(" · ") || "—"],
    ["Max parallelism", esc(rollout.max_parallelism)],
    ["Trace database", `<code>${esc(rollout.trace_database)}</code>`],
    ["Started", esc(rollout.started_at ?? "—")],
    ["Ended", esc(rollout.ended_at ?? "—")],
    ...(rollout.error ? [["Error", esc(rollout.error)]] : []),
  ]);
}

// ---------------------------------------------------------------- launching

function selectedRunMode() {
  return document.querySelector('input[name="run-mode"]:checked')?.value || "smoke";
}

async function launch(experimentId, experimentName) {
  const live = selectedRunMode() === "live";
  if (live && !confirm(
    `Run "${experimentName}" live?\n\n` +
    "This executes the full experiment against real model providers and will " +
    "incur cost. Smoke mode runs the same wiring for free."
  )) return;
  try {
    const rollout = await api("/api/rollouts", {method: "POST", body: JSON.stringify({
      experiment_id: experimentId, max_parallelism: 1, smoke_test: !live,
    })});
    $("#message").textContent = `Launched ${live ? "live" : "smoke"} rollout ${rollout.id}`;
    await refresh();
    follow(rollout.id);
  } catch (error) { $("#message").textContent = error.message; }
}

function renderRunModeNote() {
  const note = $("#run-mode-note");
  const live = selectedRunMode() === "live";
  note.classList.toggle("warn", live);
  note.textContent = live
    ? "Live runs need provider keys in the repo-root .env before docker compose up."
    : "";
  document.querySelectorAll("[data-launch]").forEach(button => {
    button.textContent = live ? "Launch live rollout" : "Launch smoke rollout";
  });
}

async function cancel(rolloutId) {
  try { await api(`/api/rollouts/${encodeURIComponent(rolloutId)}/cancel`, {method:"POST", body:"{}"}); await refresh(); }
  catch (error) { $("#message").textContent = error.message; }
}

// ---------------------------------------------------------------- following

function follow(rolloutId) {
  activeStream?.close();
  followedRollout = rolloutId;
  $("#events").textContent = "";
  selectTab("tab-rollouts");
  renderAttempts(rolloutId).catch(() => {});
  activeStream = new EventSource(`/api/rollouts/${encodeURIComponent(rolloutId)}/events`);
  // The server sends unnamed frames carrying their kind in the payload, so no
  // client-side whitelist can silently swallow a newly added event kind.
  activeStream.onmessage = event => append(event.data);
  activeStream.onerror = () => { activeStream?.close(); refresh().catch(() => {}); };
}

function append(line) {
  const output = $("#events");
  let kind = "", detail = line;
  try {
    const event = JSON.parse(line);
    kind = event.kind || "";
    const payload = event.payload;
    // A runner log line is already prose; re-printing its JSON wrapper only
    // makes the feed harder to read.
    detail = kind === "runner.log" && payload && typeof payload.line === "string"
      ? payload.line
      : (typeof payload === "string" ? payload : JSON.stringify(payload ?? line));
  } catch { /* keepalives and any non-JSON frame render verbatim */ }
  output.textContent += kind && kind !== "runner.log" ? `${kind}  ${detail}\n` : `${detail}\n`;
  output.scrollTop = output.scrollHeight;
  if (kind.startsWith("episode.") || kind.startsWith("rollout.")) {
    renderAttempts(followedRollout).catch(() => {});
    if (kind === "rollout.completed" || kind === "rollout.failed" || kind === "rollout.cancelled") {
      refresh().catch(() => {});
    }
  }
}

// A rollout is only useful once a researcher can reach what it produced: the
// persisted trace and the replayable event stream.
async function renderAttempts(rolloutId) {
  if (!rolloutId) return;
  const detail = await api(`/api/rollouts/${encodeURIComponent(rolloutId)}`);
  $("#attempts").innerHTML = detail.episode_attempts.map(attempt => {
    const stream = encodeURIComponent(attempt.redis_stream);
    const running = attempt.status === "RUNNING";
    const replay = `<a href="/replay.html?stream=${esc(stream)}${running ? "&follow=1" : ""}">`
      + `${running ? "Watch live" : "Replay"}</a>`;
    const gameId = attempt.trace_uri ? attempt.trace_uri.split("#")[1] : null;
    const trace = gameId
      ? `<a href="/trace.html?trace=${esc(encodeURIComponent(gameId))}">Open trace</a>
         <br><code>${esc(attempt.trace_uri)}</code>`
      : `<span class="muted">no trace recorded</span>`;
    return `<div class="row"><span class="status ${esc(attempt.status)}">${esc(attempt.status)}</span>
      <strong>${esc(attempt.episode_id)}</strong><br>${trace}
      ${attempt.error ? `<br><span class="status FAILED">${esc(attempt.error)}</span>` : ""}
      <br>${replay}</div>`;
  }).join("") || "No episode attempts.";
}

// ---------------------------------------------------------------- wiring

$("#release").addEventListener("change", renderConfigs);
$("#yaml-path").addEventListener("change", renderConfigPreview);
document.querySelectorAll('input[name="run-mode"]').forEach(
  input => input.addEventListener("change", () => {
    renderRunModeNote();
    renderLaunchReadiness(visibleExperiments).catch(() => {});
  }));

$("#experiment-form").addEventListener("submit", async event => {
  event.preventDefault();
  try {
    await api("/api/experiments", {method: "POST", body: JSON.stringify({
      release_id: $("#release").value,
      yaml_path: $("#yaml-path").value,
      name: $("#experiment-name").value || undefined,
    })});
    $("#message").textContent = "Experiment registered.";
    await refresh();
  } catch (error) { $("#message").textContent = error.message; }
});

// Surfacing this matters: a swallowed error would leave the panel reading
// "Loading…" forever, which looks like a slow request rather than a failure.
renderAgentPool().catch(error => { $("#agent-pool").textContent = error.message; });
refresh().catch(error => { $("#message").textContent = error.message; });
