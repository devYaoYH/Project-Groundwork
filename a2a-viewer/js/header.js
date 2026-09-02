// Renders the trace header panel: ids, times, agents, metrics.

function row(dt, dd) {
  const a = document.createElement("dt"); a.textContent = dt;
  const b = document.createElement("dd"); b.textContent = dd;
  return [a, b];
}

export function renderHeader(trace) {
  const root = document.createElement("div");
  const h2 = document.createElement("h2"); h2.textContent = "Trace"; root.appendChild(h2);

  const dl = document.createElement("dl"); dl.className = "kv";
  dl.append(...row("game_id", trace.game_id ?? "?"));
  dl.append(...row("game_name", trace.config?.game_name ?? "?"));
  dl.append(...row("experiment", trace.config?.experiment_name ?? "—"));
  if (trace.environment?.id) {
    dl.append(...row("environment", `${trace.environment.id}@${trace.environment.revision ?? "?"}`));
    dl.append(...row("environment_sha256", trace.environment.content_sha256 ?? "—"));
  }
  if (trace.episode?.id) {
    dl.append(...row("episode", trace.episode.id));
  }
  dl.append(...row("started_at", trace.started_at ?? "—"));
  dl.append(...row("ended_at", trace.ended_at ?? "—"));
  root.appendChild(dl);

  // Agents
  const agents = trace.config?.agents ?? [];
  if (agents.length) {
    const h3 = document.createElement("h2");
    h3.textContent = "Agents"; h3.style.marginTop = "12px";
    root.appendChild(h3);
    for (const a of agents) {
      const r = document.createElement("div");
      r.className = "agent-row";
      r.textContent = `· type=${a.type ?? "?"}  model=${a.model ?? "—"}`;
      root.appendChild(r);
    }
  }

  // Metrics
  const metrics = trace.metrics ?? {};
  if (Object.keys(metrics).length) {
    const h3 = document.createElement("h2");
    h3.textContent = "Metrics"; h3.style.marginTop = "12px";
    root.appendChild(h3);
    const dl2 = document.createElement("dl"); dl2.className = "kv";
    for (const [k, v] of Object.entries(metrics)) {
      dl2.append(...row(k, JSON.stringify(v)));
    }
    root.appendChild(dl2);
  }

  // Communication stats, derived for older traces when metrics are missing.
  const dmEvents = (trace.events ?? []).filter((e) => e.type === "dm_sent");
  if (dmEvents.length) {
    const lengths = dmEvents.map((e) => {
      const data = e.data ?? {};
      if (Number.isFinite(data.content_chars)) return Number(data.content_chars);
      return String(data.content ?? "").length;
    });
    const totalChars = metrics.total_dm_chars ?? lengths.reduce((a, b) => a + b, 0);
    const avgChars = metrics.avg_dm_chars ?? (totalChars / dmEvents.length);
    const maxChars = metrics.max_dm_chars ?? Math.max(...lengths);
    const perMeeting = metrics.dm_chars_per_meeting ?? (
      metrics.meetings_scheduled ? totalChars / metrics.meetings_scheduled : "—"
    );
    const h3 = document.createElement("h2");
    h3.textContent = "Communication";
    h3.style.marginTop = "12px";
    root.appendChild(h3);
    const dlComm = document.createElement("dl");
    dlComm.className = "kv";
    dlComm.append(...row("dm_count", dmEvents.length));
    dlComm.append(...row("total_dm_chars", JSON.stringify(totalChars)));
    dlComm.append(...row("avg_dm_chars", JSON.stringify(avgChars)));
    dlComm.append(...row("max_dm_chars", JSON.stringify(maxChars)));
    dlComm.append(...row("dm_chars_per_meeting", JSON.stringify(perMeeting)));
    root.appendChild(dlComm);
  }

  // Final state
  const fs = trace.final_state ?? {};
  if (Object.keys(fs).length) {
    const h3 = document.createElement("h2");
    h3.textContent = "Final state"; h3.style.marginTop = "12px";
    root.appendChild(h3);
    const dl3 = document.createElement("dl"); dl3.className = "kv";
    for (const [k, v] of Object.entries(fs)) {
      dl3.append(...row(k, JSON.stringify(v)));
    }
    root.appendChild(dl3);
  }
  return root;
}
