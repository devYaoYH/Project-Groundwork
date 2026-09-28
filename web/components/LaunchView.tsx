"use client";

import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { useEffect, useMemo, useState } from "react";

import { Chip } from "./Chip";
import { Crumb } from "./Crumb";
import { DataTable } from "./DataTable";
import {
  AttemptProgress, getLaunch, LaunchAttempt, LaunchDetail, LaunchLog,
  subscribeLaunchEvents, TERMINAL_LAUNCH_EVENTS,
} from "../lib/api";

const STATUS_TONE: Record<string, "plain" | "good" | "warn"> = {
  COMPLETED: "good", DRY_RUN: "good", PARTIAL: "warn", FAILED: "warn", CANCELLED: "warn", UNREPORTED: "warn",
};

// An attempt the control plane has not settled yet is the only one a live
// frame may speak for. Once the row is terminal the persisted verdict wins,
// which is what keeps a stale "COMPLETED" frame from outranking the settled
// UNREPORTED it turned into.
const UNSETTLED = new Set(["QUEUED", "RUNNING"]);

function timestamp(value: string | null): string { return value ? new Date(value).toLocaleString() : "-"; }

function logLine(entry: LaunchLog): string {
  return `[${timestamp(entry.created_at)}] ${entry.payload.line ?? `${entry.kind} ${JSON.stringify(entry.payload)}`}`;
}

export function LaunchView() {
  const id = (useSearchParams().get("id") || "").trim();
  const [detail, setDetail] = useState<LaunchDetail | null>(null);
  const [error, setError] = useState<string | null>(null);
  // What the durable record has said since this page opened. Kept beside the
  // fetched detail rather than merged into it so a re-read of the settled
  // launch always replaces a derivation rather than being merged with one.
  const [observed, setObserved] = useState<Record<string, AttemptProgress>>({});
  const [streamed, setStreamed] = useState<LaunchLog[]>([]);

  // The one-shot read stays exactly as it was: it is the initial state, and it
  // is the whole view for a browser with no EventSource.
  useEffect(() => {
    if (!id) return;
    setDetail(null); setError(null); setObserved({}); setStreamed([]);
    getLaunch(id).then(setDetail).catch((reason: Error) => setError(reason.message));
  }, [id]);

  useEffect(() => {
    if (!id) return;
    let stopped = false;
    let close = () => {};
    close = subscribeLaunchEvents(id, (entry) => {
      if (stopped) return;
      setStreamed((prior) => [...prior, entry]);
      if (entry.kind === "attempt.progress") {
        const progress = entry.payload as unknown as AttemptProgress;
        if (progress.episode_id) setObserved((prior) => ({ ...prior, [progress.episode_id]: progress }));
      }
      if (TERMINAL_LAUNCH_EVENTS.has(entry.kind)) {
        // The settled record is one fetch away and cannot disagree with
        // itself, so stop deriving and read it rather than assembling a final
        // state out of frames.
        stopped = true; close();
        getLaunch(id).then(setDetail).catch(() => undefined);
      }
    });
    return () => { stopped = true; close(); };
  }, [id]);

  const logs = useMemo(() => {
    // Both halves are rows of the same table read twice, so identity is the
    // row id and a frame that arrived before the fetch returned is not a
    // duplicate line.
    const merged = new Map<number, LaunchLog>();
    for (const entry of detail?.runner_logs ?? []) merged.set(entry.id, entry);
    for (const entry of streamed) merged.set(entry.id, entry);
    return [...merged.values()].sort((left, right) => left.id - right.id);
  }, [detail, streamed]);

  if (!id) return <p className="notice notice-error">Choose a launch from an experiment.</p>;
  if (error) return <p className="notice notice-error">{error}</p>;
  if (!detail) return <p className="empty-state">Loading launch...</p>;
  const { launch, attempts, progress } = detail;
  const liveOf = (attempt: LaunchAttempt): AttemptProgress | null =>
    (UNSETTLED.has(attempt.status) ? observed[attempt.episode_id] : undefined) ?? null;
  const statusOf = (attempt: LaunchAttempt): string => liveOf(attempt)?.status ?? attempt.status;
  const episodeUidOf = (attempt: LaunchAttempt): string | null =>
    attempt.episode_uid ?? liveOf(attempt)?.episode_uid ?? null;
  const completed = Math.max(
    progress.completed, attempts.filter((attempt) => statusOf(attempt) === "COMPLETED").length,
  );
  const plannedReplicationsByCell = Object.fromEntries(
    progress.by_cell.map((cell) => [cell.cell_id, cell.planned]),
  );
  return <>
    <Crumb items={[{ label: "experiments", href: "/experiments/" }, { label: "experiment", href: `/experiment/?id=${encodeURIComponent(launch.experiment_id)}` }, { label: "launch" }]} />
    <header className="page-heading page-heading-split"><div><h1>Launch inspection</h1><p className="mono">{launch.id}</p></div><Chip tone={STATUS_TONE[launch.status] ?? "plain"}>{launch.status}</Chip></header>
    {launch.error ? <p className="notice notice-error">{launch.error}</p> : null}
    <section className="metric-grid"><div className="metric"><span>mode</span><strong>{launch.mode.replace("_", " ")}</strong></div><div className="metric"><span>progress</span><strong>{completed}/{progress.planned}</strong></div><div className="metric"><span>failed</span><strong>{progress.failed}</strong></div><div className="metric"><span>shards</span><strong>{launch.shard_count ?? 1}</strong></div><div className="metric"><span>parallelism / shard</span><strong>{launch.max_parallelism}</strong></div></section>
    <section className="card section-card"><div className="section-heading"><h2>Launch record</h2><p>Created {timestamp(launch.created_at)} · started {timestamp(launch.started_at)} · ended {timestamp(launch.ended_at)}</p></div><dl className="launch-metadata"><dt>experiment</dt><dd><Link href={`/experiment/?id=${encodeURIComponent(launch.experiment_id)}`}>{launch.experiment_id}</Link></dd><dt>trace database</dt><dd className="mono">{launch.trace_database}</dd><dt>execution plan</dt><dd className="mono">{launch.execution_path ?? "-"}</dd></dl></section>
    <section className="card section-card"><div className="section-heading"><h2>Episodes</h2><p>One row per replication in this launch, advancing as the control plane observes durable evidence. Open the cell trace history to inspect earlier physical runs; dry runs intentionally have no trace.</p></div><DataTable rows={attempts} rowKey={(attempt) => attempt.id} columns={[
      { label: "episode", render: (attempt) => { const uid = episodeUidOf(attempt); return uid ? <Link className="table-link" href={`/episode/?uid=${encodeURIComponent(uid)}`}><strong>{attempt.episode_id}</strong><span>Open episode</span></Link> : <span className="mono">{attempt.episode_id}</span>; } },
      { label: "cell", className: "mono", render: (attempt) => attempt.cell_id }, { label: "replication", className: "mono", render: (attempt) => `${attempt.episode_idx + 1} / ${plannedReplicationsByCell[attempt.cell_id] ?? "-"}` },
      { label: "execution", className: "mono", render: (attempt) => attempt.execution ?? "-" },
      { label: "trace history", render: (attempt) => <Link className="text-link" href={`/episodes/?cell_id=${encodeURIComponent(attempt.cell_id)}`}>View cell traces</Link> },
      { label: "status", render: (attempt) => <Chip tone={STATUS_TONE[statusOf(attempt)] ?? "plain"}>{statusOf(attempt)}</Chip> },
      // Durable events, not emitted ones: the count is how far the shared
      // record reached, which is also how much of this episode would survive
      // its worker being killed right now.
      { label: "diagnostic", render: (attempt) => attempt.error ?? (liveOf(attempt) ? `${liveOf(attempt)!.durable_through} durable events` : attempt.status === "DRY_RUN" ? "Validated by runner; no episode trace is persisted." : attempt.episode_uri ?? "-") },
    ]} /></section>
    <section className="card section-card"><div className="section-heading"><h2>Launch events</h2><p>The control plane&apos;s own record of what it did and what it then observed, appended live and still here after reopening this launch.</p></div>{logs.length ? <pre className="runner-log">{logs.map(logLine).join("\n")}</pre> : <p className="empty-state">No launch events were recorded for this launch.</p>}</section>
  </>;
}
