// One replay shell for every game.
//
// The left panel is the same for all of them — projection status and the raw
// event log as it arrives — and the stage holds whichever viewer that game
// ships. Games render their own episodes very differently; what a researcher
// should not have to relearn per game is where the controls and the events are.

const $ = id => document.getElementById(id);

// Each game's viewer is its own page, so the stage embeds it rather than
// splicing foreign markup and stylesheets into this one.
const VIEWERS = {
  calendar: stream =>
    `/game-assets/calendar/tasks/viewer.html?trace=${encodeURIComponent(traceUrl(stream))}`,
  negotiation: stream =>
    `/game-replays/negotiation/?embed=1&stream=${encodeURIComponent(stream)}`,
  buyer_seller: stream => `/game-replays/buyer-seller/?embed=1&stream=${encodeURIComponent(stream)}`,
  word_guess: stream => `/game-replays/word-guess/?embed=1&stream=${encodeURIComponent(stream)}`,
};

const FOLLOW_INTERVAL_MS = 2000;

let shownEvents = 0;
let followTimer = null;
let mountedFor = null;

function traceUrl(stream) {
  return `/api/streams/${encodeURIComponent(stream)}/trace`;
}

function esc(value) {
  return String(value ?? "").replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

function summarise(event) {
  const data = event.data || {};
  for (const key of ["message", "phase", "speaker", "agent_id", "meeting_id", "round"]) {
    if (data[key] !== undefined && data[key] !== null) return `${key}=${data[key]}`;
  }
  const keys = Object.keys(data);
  return keys.length ? `${keys.length} field${keys.length === 1 ? "" : "s"}` : "";
}

function appendEvents(events) {
  const log = $("log");
  const atBottom = log.scrollHeight - log.scrollTop - log.clientHeight < 40;
  const fragment = document.createDocumentFragment();

  for (let i = shownEvents; i < events.length; i++) {
    const row = document.createElement("div");
    row.className = shownEvents ? "row fresh" : "row";
    row.innerHTML =
      `<span class="n">${i}</span>` +
      `<span><span class="t">${esc(events[i].type)}</span> ` +
      `<span class="d">${esc(summarise(events[i]))}</span></span>`;
    fragment.appendChild(row);
  }
  log.appendChild(fragment);
  shownEvents = events.length;
  if (atBottom) log.scrollTop = log.scrollHeight;
}

function mountViewer(game, stream) {
  const key = `${game}::${stream}`;
  if (mountedFor === key) return;          // never reload the viewer under the user
  const build = VIEWERS[game];
  const stage = $("stage");
  if (!build) {
    stage.innerHTML = `<div class="empty">No viewer is registered for ${esc(game)}.</div>`;
    mountedFor = key;
    return;
  }
  stage.innerHTML = "";
  const frame = document.createElement("iframe");
  frame.src = build(stream);
  frame.title = `${game} viewer`;
  stage.appendChild(frame);
  mountedFor = key;
}

async function load(stream, { reset = true } = {}) {
  if (!stream) { $("stat").textContent = "Enter a stream id."; return; }
  if (reset) { shownEvents = 0; $("log").innerHTML = ""; mountedFor = null; }

  try {
    const response = await fetch(traceUrl(stream));
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.error || `HTTP ${response.status}`);

    const game = payload.config?.game_name || "unknown";
    $("game").textContent = game;
    document.title = `${game} replay`;

    const { partial, event_count } = payload.projection;
    const stat = $("stat");
    stat.classList.toggle("partial", partial);
    stat.textContent = partial
      ? `Still running — ${event_count} events so far.`
      : `Complete — ${event_count} events.`;

    appendEvents(payload.events || []);
    mountViewer(game, stream);

    // Nothing further will arrive once the episode has reached its end.
    if (!partial && $("follow").checked) {
      $("follow").checked = false;
      setFollow(false);
    }
  } catch (error) {
    $("stat").classList.remove("partial");
    $("stat").textContent = error.message;
  }
}

function setFollow(on) {
  clearInterval(followTimer);
  followTimer = null;
  if (!on) return;
  // Poll for events appended since the last read; the viewer keeps its own
  // position rather than being remounted underneath the reader.
  followTimer = setInterval(
    () => load($("stream").value.trim(), { reset: false }),
    FOLLOW_INTERVAL_MS,
  );
}

function toggleCollapse() {
  const split = $("split");
  const collapsed = split.classList.toggle("collapsed");
  const handle = $("handle");
  handle.textContent = collapsed ? "›" : "‹";
  const label = collapsed ? "Expand event panel" : "Collapse event panel";
  handle.title = label;
  handle.setAttribute("aria-label", label);
}

$("load").addEventListener("click", () => load($("stream").value.trim()));
$("refresh").addEventListener("click", () => load($("stream").value.trim(), { reset: false }));
$("follow").addEventListener("change", event => setFollow(event.target.checked));
$("handle").addEventListener("click", toggleCollapse);
$("handle").addEventListener("keydown", event => {
  if (event.key === "Enter" || event.key === " ") { event.preventDefault(); toggleCollapse(); }
});
$("stream").addEventListener("keydown", event => {
  if (event.key === "Enter") load($("stream").value.trim());
});

const params = new URLSearchParams(location.search);
const initial = params.get("stream");
if (initial) {
  $("stream").value = initial;
  if (params.get("follow") === "1") { $("follow").checked = true; setFollow(true); }
  load(initial);
}
