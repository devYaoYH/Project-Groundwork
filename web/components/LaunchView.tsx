"use client";

import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { useEffect, useState } from "react";

import { Chip } from "./Chip";
import { Crumb } from "./Crumb";
import { DataTable } from "./DataTable";
import { getLaunch, LaunchDetail, streamLaunchEvents } from "../lib/api";
import { ACTIVE_LAUNCH_STATUSES, boundedLaunchDetail, LaunchStreamError, updateLaunchDetail } from "../lib/launch-stream";
import { notifySessionExpired } from "../lib/session";

const STATUS_TONE: Record<string, "plain" | "good" | "warn"> = {
  COMPLETED: "good", DRY_RUN: "good", PARTIAL: "warn", FAILED: "warn", CANCELLED: "warn", UNREPORTED: "warn",
};

function timestamp(value: string | null): string { return value ? new Date(value).toLocaleString() : "-"; }

export function LaunchView() {
  const id = (useSearchParams().get("id") || "").trim();
  const [detail, setDetail] = useState<LaunchDetail | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [streamError, setStreamError] = useState<string | null>(null);
  const [connection, setConnection] = useState("Connecting live logs…");
  useEffect(() => {
    if (!id) return;
    setDetail(null); setError(null); setStreamError(null); setConnection("Connecting live logs…");
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout>;
    const controller = new AbortController();
    let cursor = 0;
    let terminal = false;
    let retries = 0;
    let refreshing = false;
    let refreshAgain = false;
    const refreshLocalSnapshot = async () => {
      if (cancelled) return;
      if (refreshing) { refreshAgain = true; return; }
      refreshing = true;
      try {
        const next = await getLaunch(id);
        if (cancelled) return;
        setDetail(current => boundedLaunchDetail({ ...next, runner_logs: [...(current?.runner_logs ?? []), ...next.runner_logs], logs_truncated: Boolean(current?.logs_truncated || next.logs_truncated) }));
      } catch (reason) { if (!cancelled) setStreamError(reason instanceof Error ? reason.message : "Unable to refresh launch progress"); }
      finally {
        refreshing = false;
        if (refreshAgain && !cancelled) { refreshAgain = false; void refreshLocalSnapshot(); }
      }
    };
    const connect = async () => {
      if (cancelled || terminal) return;
      setConnection(retries ? "Reconnecting live logs…" : "Connecting live logs…");
      try {
        await streamLaunchEvents(id, { signal: controller.signal, afterId: cursor, onEvent: event => {
          if (cancelled) return false;
          if (event.kind === "auth.expired") {
            terminal = true; setConnection("Sign in to resume live logs"); notifySessionExpired(); return false;
          }
          retries = 0; setStreamError(null); setConnection("Live updates connected");
          if (event.id !== null) cursor = Math.max(cursor, event.id);
          setDetail(current => current ? updateLaunchDetail(current, event) : current);
          if (event.kind === "launch.status" && typeof event.data.status === "string") {
            terminal = !ACTIVE_LAUNCH_STATUSES.has(event.data.status);
          } else if (event.kind.startsWith("episode.") || event.kind.startsWith("launch.") && event.kind !== "launch.progress") {
            // The original local server emits lifecycle events rather than
            // progress snapshots. Refresh only when one of those changes.
            void refreshLocalSnapshot();
            terminal = ["launch.completed", "launch.failed", "launch.cancelled", "launch.partial", "launch.unreported", "launch.dry_run"].includes(event.kind);
          }
          if (terminal) { setConnection("Launch ended · logs saved"); return false; }
        } });
      } catch (reason) {
        if (cancelled || controller.signal.aborted) return;
        setStreamError(reason instanceof Error ? reason.message : "Live logs disconnected.");
        if (reason instanceof LaunchStreamError && [401, 403, 404].includes(reason.status ?? 0)) {
          setConnection("Live logs unavailable"); return;
        }
      }
      if (!cancelled && !terminal) {
        retries += 1;
        setConnection("Reconnecting live logs…");
        timer = setTimeout(() => void connect(), Math.min(10000, 1000 * 2 ** Math.min(retries, 3)));
      }
    };
    const start = async () => {
      try {
        const next = await getLaunch(id);
        if (cancelled) return;
        const bounded = boundedLaunchDetail(next);
        cursor = Math.max(0, ...bounded.runner_logs.map(log => log.id));
        setDetail(bounded);
        terminal = !ACTIVE_LAUNCH_STATUSES.has(next.launch.status);
        if (terminal) setConnection("Launch ended · logs saved");
        else void connect();
      } catch (reason) { if (!cancelled) setError(reason instanceof Error ? reason.message : "Unable to load launch"); }
    };
    void start();
    return () => { cancelled = true; terminal = true; controller.abort(); clearTimeout(timer); };
  }, [id]);
  if (!id) return <p className="notice notice-error">Choose a launch from an experiment.</p>;
  if (error) return <p className="notice notice-error">{error}</p>;
  if (!detail) return <p className="empty-state">Loading launch...</p>;
  const { launch, attempts, progress, runner_logs: logs } = detail;
  const plannedReplicationsByCell = Object.fromEntries(
    progress.by_cell.map((cell) => [cell.cell_id, cell.planned]),
  );
  return <>
    <Crumb items={[{ label: "experiments", href: "/experiments/" }, { label: "experiment", href: `/experiment/?id=${encodeURIComponent(launch.experiment_id)}` }, { label: "launch" }]} />
    <header className="page-heading page-heading-split"><div><h1>Launch inspection</h1><p className="mono">{launch.id}</p></div><Chip tone={STATUS_TONE[launch.status] ?? "plain"}>{launch.status}</Chip></header>
    {launch.error ? <p className="notice notice-error">{launch.error}</p> : null}
    {detail.provisional ? <p className="notice notice-muted">Live progress is provisional. Episode traces and downloadable results become available once the launch finishes saving.</p> : null}
    {launch.live_logs === false && ACTIVE_LAUNCH_STATUSES.has(launch.status) ? <p className="notice notice-muted">This experiment uses an earlier release; runner logs become available after the launch ends. Fork the design to use live logs.</p> : null}
    <section className="metric-grid"><div className="metric"><span>mode</span><strong>{launch.mode.replace("_", " ")}</strong></div><div className="metric"><span>progress</span><strong>{progress.completed}/{progress.planned}</strong></div><div className="metric"><span>failed</span><strong>{progress.failed}</strong></div><div className="metric"><span>parallelism</span><strong>{launch.max_parallelism}</strong></div></section>
    <section className="card section-card"><div className="section-heading"><h2>Launch record</h2><p>Created {timestamp(launch.created_at)} · started {timestamp(launch.started_at)} · ended {timestamp(launch.ended_at)}</p></div><dl className="launch-metadata"><dt>experiment</dt><dd><Link href={`/experiment/?id=${encodeURIComponent(launch.experiment_id)}`}>{launch.experiment_id}</Link></dd><dt>trace database</dt><dd className="mono">{launch.trace_database}</dd><dt>execution plan</dt><dd className="mono">{launch.execution_path ?? "-"}</dd></dl></section>
    <section className="card section-card"><div className="section-heading"><h2>Episodes</h2><p>One row per replication in this launch. Open the cell trace history to inspect earlier physical runs; dry runs intentionally have no trace.</p></div><DataTable rows={attempts} rowKey={(attempt) => attempt.id} columns={[
      { label: "episode", render: (attempt) => attempt.episode_uid && !detail.provisional ? <Link className="table-link" href={`/episode/?uid=${encodeURIComponent(attempt.episode_uid)}`}><strong>{attempt.episode_id}</strong><span>Open episode</span></Link> : <span className="mono">{attempt.episode_id}</span> },
      { label: "cell", className: "mono", render: (attempt) => attempt.cell_id }, { label: "replication", className: "mono", render: (attempt) => `${attempt.episode_idx + 1} / ${plannedReplicationsByCell[attempt.cell_id] ?? "-"}` },
      { label: "trace history", render: (attempt) => <Link className="text-link" href={`/episodes/?cell_id=${encodeURIComponent(attempt.cell_id)}`}>View cell traces</Link> },
      { label: "status", render: (attempt) => <Chip tone={STATUS_TONE[attempt.status] ?? "plain"}>{attempt.status}</Chip> },
      { label: "diagnostic", render: (attempt) => attempt.error ?? (attempt.status === "DRY_RUN" ? "Validated by runner; no episode trace is persisted." : attempt.episode_uri ?? "-") },
    ]} /></section>
    <section className="card section-card"><div className="section-heading"><h2>Runner log</h2><p role="status">{connection}. Showing up to 1,000 recent lines; persisted logs remain available after the launch ends.</p></div>{detail.logs_truncated ? <p className="notice notice-muted">Earlier output is omitted here. Download the results after completion for the full runner log.</p> : null}{streamError ? <p className="notice notice-error" role="alert">{streamError} Existing output is preserved.</p> : null}{logs.length ? <pre className="runner-log" aria-label="Runner output">{logs.map((entry) => `[${timestamp(entry.created_at)}] ${entry.payload.line ?? ""}`).join("\n")}</pre> : <p className="empty-state">{ACTIVE_LAUNCH_STATUSES.has(launch.status) ? launch.status === "RUNNING" ? "Runner active. Waiting for its first output." : "Waiting for the runner to start. Output will appear here as it is written." : "No runner output was recorded for this launch."}</p>}</section>
  </>;
}
