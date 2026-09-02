const game = document.body.dataset.game;
const $ = selector => document.querySelector(selector);
let entries = [];
let cursor = 0;
let timer = null;

function esc(value) {
  return String(value ?? "").replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;");
}

function render() {
  const visible = entries.slice(0, cursor);
  $("#count").textContent = `${visible.length}/${entries.length} events`;
  $("#timeline").innerHTML = visible.map(({stream_id, event}) => `
    <article><small>${esc(stream_id)} · ${esc(event.timestamp)}</small><strong>${esc(event.type)}</strong><pre>${esc(JSON.stringify(event.data, null, 2))}</pre></article>`).join("") || "<p>No events replayed yet.</p>";
  $("#timeline").scrollTop = $("#timeline").scrollHeight;
}

function stop() { if (timer) clearInterval(timer); timer = null; }
function play() {
  stop();
  timer = setInterval(() => { if (cursor >= entries.length) return stop(); cursor += 1; render(); }, 250);
}

async function load() {
  stop(); cursor = 0; render();
  const stream = $("#stream").value.trim();
  if (!stream) return;
  const response = await fetch(`/api/streams/${encodeURIComponent(stream)}`);
  const payload = await response.json();
  if (!payload.available) throw new Error(payload.error || "Redis stream is unavailable");
  entries = payload.events || [];
  $("#status").textContent = `${game}: loaded ${entries.length} event(s) from Redis stream.`;
  render();
}

$("#load").onclick = () => load().catch(error => { $("#status").textContent = error.message; });
$("#play").onclick = play;
$("#step").onclick = () => { stop(); cursor = Math.min(entries.length, cursor + 1); render(); };
$("#reset").onclick = () => { stop(); cursor = 0; render(); };
const stream = new URLSearchParams(location.search).get("stream");
if (stream) { $("#stream").value = stream; load().catch(error => { $("#status").textContent = error.message; }); }
