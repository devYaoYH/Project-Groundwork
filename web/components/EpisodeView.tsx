"use client";

import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { useEffect, useMemo, useRef, useState } from "react";

import { Chip } from "./Chip";
import { ConfigDisclosure } from "./ConfigDisclosure";
import { Crumb } from "./Crumb";
import { EpisodeDetail, getEpisode } from "../lib/api";
import { specialisedViewerUrl } from "../lib/environments";
import { laneHue, projectLanes, projectTranscript } from "../lib/lanes";

const STATUS_TONE: Record<string, "plain" | "good" | "warn"> = {
  COMPLETED: "good",
  PARTIAL: "warn",
  STOPPED: "warn",
};

function record(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : {};
}

function text(value: unknown): string | null {
  return typeof value === "string" && value ? value : null;
}

export function EpisodeView() {
  const search = useSearchParams();
  const uid = (search.get("uid") || "").trim();

  const [detail, setDetail] = useState<EpisodeDetail | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [cursor, setCursor] = useState(0);
  const [playing, setPlaying] = useState(false);
  const viewer = useRef<HTMLIFrameElement | null>(null);
  const activeLine = useRef<HTMLLIElement | null>(null);

  useEffect(() => {
    if (!uid) return;
    setDetail(null);
    setError(null);
    getEpisode(uid)
      .then((next) => {
        setDetail(next);
        // Open on the finished episode. A researcher arrives here asking what
        // happened, not what happened first.
        setCursor(Math.max(0, (next.cursor_max ?? 0) - 1));
      })
      .catch((reason: Error) => setError(reason.message));
  }, [uid]);

  const episode = detail?.episode ?? null;
  const provenance = record(record(episode?.config).provenance);

  const projection = useMemo(
    () => projectLanes(episode?.events, detail?.lanes, detail?.index_label),
    [episode?.events, detail?.lanes, detail?.index_label],
  );
  const transcript = useMemo(
    () => projectTranscript(episode?.events, detail?.lanes),
    [episode?.events, detail?.lanes],
  );
  const cursorMax = projection.cursorMax;
  const bounded = Math.min(cursor, Math.max(0, cursorMax - 1));

  // One cursor drives everything, including a viewer the environment shipped
  // and this app knows nothing else about.
  useEffect(() => {
    viewer.current?.contentWindow?.postMessage(
      { type: "cursor", index: bounded + 1 },
      window.location.origin,
    );
  }, [bounded, detail]);

  useEffect(() => {
    activeLine.current?.scrollIntoView({ block: "nearest" });
  }, [bounded]);

  useEffect(() => {
    if (!playing || cursorMax === 0) return;
    const timer = setInterval(() => {
      setCursor((current) => {
        if (current >= cursorMax - 1) {
          setPlaying(false);
          return current;
        }
        return current + 1;
      });
    }, 220);
    return () => clearInterval(timer);
  }, [playing, cursorMax]);

  if (!uid) return <p className="notice notice-error">Choose an episode from the episode list.</p>;
  if (error) return <p className="notice notice-error">{error}</p>;
  if (!episode) return <p className="empty-state">Loading episode...</p>;

  const environmentId = text(record(episode.config).environment_id);
  const experimentName = text(provenance.experiment_name) ?? text(record(episode.config).experiment_name);
  const experimentId = text(provenance.experiment_id);
  const cellId = text(provenance.cell_id);
  const status = episode.observability?.partial ? "PARTIAL" : episode.stopped ? "STOPPED" : "COMPLETED";
  const viewerUrl = specialisedViewerUrl(environmentId, episode.episode_uid);
  const metrics = Object.entries(record(episode.metrics));

  return (
    <>
      <Crumb
        items={[
          { label: "experiments", href: "/experiments/" },
          experimentId
            ? { label: experimentName ?? experimentId, href: `/experiment/?id=${encodeURIComponent(experimentId)}` }
            : { label: experimentName ?? "unattributed", href: "/episodes/" },
          { label: cellId ?? "no cell" },
          { label: episode.episode_uid },
        ]}
      />
      <header className="page-heading page-heading-split">
        <div>
          <h1>{text(provenance.episode_id) ?? episode.episode_uid}</h1>
          <p>
            {environmentId ?? "unknown environment"} · attempt {String(provenance.attempt ?? 1)} ·
            seed {String(record(episode.config).seed ?? provenance.seed ?? "-")}
          </p>
        </div>
        <Chip tone={STATUS_TONE[status] ?? "plain"}>{status}</Chip>
      </header>

      {metrics.length > 0 ? (
        <section className="metric-grid episode-measures">
          {metrics.slice(0, 6).map(([name, value]) => (
            <div className="metric" key={name}>
              <span>{name}</span>
              <strong>{typeof value === "object" ? JSON.stringify(value) : String(value)}</strong>
            </div>
          ))}
        </section>
      ) : null}

      <section className="card section-card">
        <div className="section-heading">
          <h2>Lanes</h2>
          <p>
            One row per participant. A tall block is a committed action, a slim one an utterance.
            Colour distinguishes participants within this view only.
          </p>
        </div>
        {cursorMax === 0 ? (
          <p className="empty-state">
            This attempt recorded no events. That is a state, not a gap: the episode ended before
            its first turn.
          </p>
        ) : (
          <>
            <div className="lane-scroll">
              <div
                className="lane-grid"
                style={{ gridTemplateColumns: `9rem repeat(${cursorMax}, minmax(9px, 1fr))` }}
              >
                {projection.separators.map((separator) => (
                  <div
                    aria-hidden
                    className="lane-separator"
                    key={`separator-${separator.cursor}`}
                    style={{ gridColumn: separator.cursor + 2, gridRow: `1 / ${projection.rows.length + 2}` }}
                  >
                    <span>{separator.label} {String(separator.value)}</span>
                  </div>
                ))}
                <div
                  aria-hidden
                  className="lane-cursor"
                  style={{ gridColumn: bounded + 2, gridRow: `1 / ${projection.rows.length + 2}` }}
                />
                {projection.rows.map((row, index) => (
                  <div className="lane-label" key={`label-${row.participant_id}`} style={{ gridRow: index + 2 }}>
                    <strong style={{ color: `hsl(${laneHue(projection.rows, row.participant_id)} 42% 42%)` }}>
                      {row.participant_id}
                    </strong>
                    <span>{row.declared ? row.binding ?? row.kind : "from events"}</span>
                  </div>
                ))}
                {projection.rows.map((row, index) =>
                  row.marks.map((mark) => (
                    <button
                      className={`lane-mark lane-mark-${mark.shape}${mark.cursor <= bounded ? "" : " lane-mark-ahead"}`}
                      key={`${row.participant_id}-${mark.cursor}`}
                      onClick={() => { setPlaying(false); setCursor(mark.cursor); }}
                      style={{
                        gridColumn: mark.cursor + 2,
                        gridRow: index + 2,
                        background: `hsl(${laneHue(projection.rows, row.participant_id)} 42% 46%)`,
                      }}
                      title={`${mark.cursor + 1}. ${mark.type}`}
                      type="button"
                    >
                      <span className="visually-hidden">{mark.type}</span>
                    </button>
                  )),
                )}
              </div>
            </div>
            <div className="scrubber">
              <button onClick={() => { setPlaying(false); setCursor((value) => Math.max(0, value - 1)); }} type="button">Step back</button>
              <button onClick={() => setPlaying((value) => !value)} type="button">{playing ? "Pause" : "Play"}</button>
              <button onClick={() => { setPlaying(false); setCursor((value) => Math.min(cursorMax - 1, value + 1)); }} type="button">Step</button>
              <input
                aria-label="Episode cursor"
                max={cursorMax - 1}
                min={0}
                onChange={(event) => { setPlaying(false); setCursor(Number(event.target.value)); }}
                type="range"
                value={bounded}
              />
              <span className="mono muted">{bounded + 1}/{cursorMax}</span>
            </div>
          </>
        )}
      </section>

      <div className="episode-panes">
        <section className="card section-card">
          <div className="section-heading">
            <h2>Transcript</h2>
            <p>Every recorded event in order. Chat supports {"{speaker, text}"} and Calendar's legacy content field.</p>
          </div>
          <ol className="transcript">
            {transcript.map((line) => (
              <li
                className={`transcript-line${line.cursor === bounded ? " transcript-active" : ""}`}
                key={line.cursor}
                onClick={() => { setPlaying(false); setCursor(line.cursor); }}
                ref={line.cursor === bounded ? activeLine : null}
              >
                <span className="transcript-meta">
                  <em style={{ color: `hsl(${laneHue(projection.rows, line.laneId)} 42% 42%)` }}>{line.laneId}</em>
                  <code>{line.type}</code>
                </span>
                {line.text ? <p>{line.text}</p> : <p className="muted">no text on this event</p>}
                <ConfigDisclosure
                  label="Show recorded payload"
                  value={JSON.stringify(line.data, null, 2)}
                />
              </li>
            ))}
            {transcript.length === 0 ? <p className="empty-state">Nothing was said in this attempt.</p> : null}
          </ol>
        </section>

        {viewerUrl ? (
          <section className="card section-card">
            <div className="section-heading">
              <h2>{environmentId} replay</h2>
              <p>The environment&apos;s own rendering, driven by the same cursor.</p>
            </div>
            <iframe
              className="specialised-viewer"
              onLoad={() => viewer.current?.contentWindow?.postMessage(
                { type: "cursor", index: bounded + 1 }, window.location.origin,
              )}
              ref={viewer}
              src={viewerUrl}
              title={`${environmentId} replay`}
            />
          </section>
        ) : null}
      </div>

      <section className="release-meta">
        <span>release <strong className="mono">{text(provenance.release_id) ?? "-"}</strong></span>
        <span>item <strong className="mono">{text(provenance.item_id) ?? "-"}</strong></span>
        <span>design <strong className="mono">{text(provenance.design_sha256) ?? "hand-written config"}</strong></span>
        <span>oracle <strong className="mono">{text(provenance.oracle_version) ?? "-"}</strong></span>
      </section>
      <ConfigDisclosure
        label="Show the execution config this episode consumed"
        value={JSON.stringify(episode.config ?? {}, null, 2)}
      />
      <p className="episode-footnote muted">
        Prompt fields in event payloads are the environment's recorded payloads. Calendar does not
        capture the exact provider request after client-side assembly.
      </p>
      {environmentId ? (
        <p className="episode-footnote">
          <Link className="text-link" href={`/environments/${environmentId}/`}>Open the {environmentId} declaration</Link>
        </p>
      ) : null}
    </>
  );
}
