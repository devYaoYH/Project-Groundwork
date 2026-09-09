"use client";

import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { useEffect, useState } from "react";

import { Chip } from "./Chip";
import { Crumb } from "./Crumb";
import { DataTable } from "./DataTable";
import { getLaunch, LaunchDetail } from "../lib/api";

const STATUS_TONE: Record<string, "plain" | "good" | "warn"> = {
  COMPLETED: "good", DRY_RUN: "good", PARTIAL: "warn", FAILED: "warn", CANCELLED: "warn", UNREPORTED: "warn",
};

function timestamp(value: string | null): string { return value ? new Date(value).toLocaleString() : "-"; }

export function LaunchView() {
  const id = (useSearchParams().get("id") || "").trim();
  const [detail, setDetail] = useState<LaunchDetail | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    if (!id) return;
    setDetail(null); setError(null);
    getLaunch(id).then(setDetail).catch((reason: Error) => setError(reason.message));
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
    <section className="metric-grid"><div className="metric"><span>mode</span><strong>{launch.mode.replace("_", " ")}</strong></div><div className="metric"><span>progress</span><strong>{progress.completed}/{progress.planned}</strong></div><div className="metric"><span>failed</span><strong>{progress.failed}</strong></div><div className="metric"><span>parallelism</span><strong>{launch.max_parallelism}</strong></div></section>
    <section className="card section-card"><div className="section-heading"><h2>Launch record</h2><p>Created {timestamp(launch.created_at)} · started {timestamp(launch.started_at)} · ended {timestamp(launch.ended_at)}</p></div><dl className="launch-metadata"><dt>experiment</dt><dd><Link href={`/experiment/?id=${encodeURIComponent(launch.experiment_id)}`}>{launch.experiment_id}</Link></dd><dt>trace database</dt><dd className="mono">{launch.trace_database}</dd><dt>execution plan</dt><dd className="mono">{launch.execution_path ?? "-"}</dd></dl></section>
    <section className="card section-card"><div className="section-heading"><h2>Episodes</h2><p>One row per replication in this launch. Open the cell trace history to inspect earlier physical runs; dry runs intentionally have no trace.</p></div><DataTable rows={attempts} rowKey={(attempt) => attempt.id} columns={[
      { label: "episode", render: (attempt) => attempt.episode_uid ? <Link className="table-link" href={`/episode/?uid=${encodeURIComponent(attempt.episode_uid)}`}><strong>{attempt.episode_id}</strong><span>Open episode</span></Link> : <span className="mono">{attempt.episode_id}</span> },
      { label: "cell", className: "mono", render: (attempt) => attempt.cell_id }, { label: "replication", className: "mono", render: (attempt) => `${attempt.episode_idx + 1} / ${plannedReplicationsByCell[attempt.cell_id] ?? "-"}` },
      { label: "trace history", render: (attempt) => <Link className="text-link" href={`/episodes/?cell_id=${encodeURIComponent(attempt.cell_id)}`}>View cell traces</Link> },
      { label: "status", render: (attempt) => <Chip tone={STATUS_TONE[attempt.status] ?? "plain"}>{attempt.status}</Chip> },
      { label: "diagnostic", render: (attempt) => attempt.error ?? (attempt.status === "DRY_RUN" ? "Validated by runner; no episode trace is persisted." : attempt.episode_uri ?? "-") },
    ]} /></section>
    <section className="card section-card"><div className="section-heading"><h2>Runner log</h2><p>Persisted output remains available after reopening this terminal launch.</p></div>{logs.length ? <pre className="runner-log">{logs.map((entry) => `[${timestamp(entry.created_at)}] ${entry.payload.line ?? ""}`).join("\n")}</pre> : <p className="empty-state">No runner output was recorded for this launch.</p>}</section>
  </>;
}
