"use client";

import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { useEffect, useState } from "react";

import { Chip } from "./Chip";
import { Crumb } from "./Crumb";
import { DataTable } from "./DataTable";
import { ReplicationHistory } from "./ReplicationHistory";
import { EpisodeSummary, ExperimentDetail, MetricSummary, getExperiment, listEpisodes } from "../lib/api";
import { groupReplicationExecutions, replicationProgress } from "../lib/replications";

const EPISODE_TONE: Record<string, "plain" | "good" | "warn"> = {
  COMPLETED: "good",
  PARTIAL: "warn",
  STOPPED: "warn",
};

function statusTone(status: string): "plain" | "good" | "warn" {
  return status === "COMPLETED" ? "good" : status === "NOT_STARTED" ? "plain" : "warn";
}

function metricSummary(metric: MetricSummary) {
  if (metric.kind === "boolean") {
    return `${metric.name}: ${metric.true_count ?? 0} true / ${metric.false_count ?? 0} false (n=${metric.n})`;
  }
  const parts = [`${metric.name}: mean ${metric.mean ?? "-"}`, `n=${metric.n}`];
  if (metric.min !== undefined && metric.max !== undefined) parts.push(`range ${metric.min}-${metric.max}`);
  if (metric.stddev !== undefined) parts.push(`sd ${metric.stddev}`);
  return parts.join("; ");
}

export function ExperimentDetailView() {
  const search = useSearchParams();
  const id = search.get("id") || "";
  const [detail, setDetail] = useState<ExperimentDetail | null>(null);
  const [episodes, setEpisodes] = useState<EpisodeSummary[]>([]);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!id) return;
    getExperiment(id).then(setDetail).catch((reason: Error) => setError(reason.message));
    // An experiment with no episodes yet is the normal state between locking
    // and the first launch finishing, so this failing must not blank the page.
    listEpisodes({ experiment_id: id })
      .then((page) => setEpisodes(page.episodes ?? []))
      .catch(() => setEpisodes([]));
  }, [id]);

  if (!id) return <p className="notice notice-error">Choose an experiment from the library.</p>;
  if (error) return <p className="notice notice-error">{error}</p>;
  if (!detail) return <p className="empty-state">Loading experiment...</p>;
  const experiment = detail.experiment;
  const cells = detail.cells ?? [];
  const cellEvidence = detail.cell_evidence ?? [];
  const launches = detail.launches ?? [];
  const newestLaunch = launches[0];
  const progress = newestLaunch?.progress;
  const plannedReplicationsByCell = Object.fromEntries(
    cells.map((cell) => [cell.cell_id, cell.episodes_planned]),
  );
  const replicationGroups = groupReplicationExecutions(episodes);
  return (
    <>
      <Crumb items={[{ label: "experiments", href: "/experiments/" }, { label: experiment.name }]} />
      <header className="page-heading page-heading-split">
        <div><h1>{experiment.name}</h1><p>Release <code>{experiment.release_id}</code> · {experiment.locked_at ? `locked ${new Date(experiment.locked_at).toLocaleString()}` : "draft — live execution is locked"}</p></div>
        {experiment.design_text ? <Link className="button button-primary" href={`/design/?id=${encodeURIComponent(experiment.id)}`}>Open design</Link> : null}
      </header>
      <section className="metric-grid">
        <div className="metric"><span>cells</span><strong>{cells.length}</strong></div>
        <div className="metric"><span>planned replications</span><strong>{cellEvidence.reduce((total, cell) => total + cell.planned_replicas, 0)}</strong></div>
        <div className="metric"><span>completed replications</span><strong>{cellEvidence.reduce((total, cell) => total + cell.completed_replicas, 0)}</strong></div>
        <div className="metric"><span>latest launch</span><strong>{progress ? `${progress.completed}/${progress.planned}` : "-"}</strong></div>
      </section>
      <section className="card section-card">
        <div className="section-heading"><h2>Cell evidence</h2><p>Locked levels are compared with the latest execution of each planned replication. A retry replaces its earlier execution in these summaries, not in trace history.</p></div>
        {cellEvidence.length === 0 ? <p className="empty-state">No locked cell plan is available for this experiment yet.</p> : <DataTable rows={cellEvidence} rowKey={(cell) => cell.cell_id} columns={[
          { label: "cell", className: "mono", render: (cell) => cell.cell_id },
          { label: "levels", className: "mono muted", render: (cell) => JSON.stringify(cell.levels) },
          { label: "replications", className: "mono", render: (cell) => `${cell.completed_replicas}/${cell.planned_replicas} completed` },
          { label: "latest status", render: (cell) => <span className="evidence-chips">{Object.entries(cell.status_counts).map(([status, count]) => <Chip key={status} tone={statusTone(status)}>{status} {count}</Chip>)}</span> },
          { label: "native measures", render: (cell) => cell.metric_summaries.length ? <span className="evidence-metrics">{cell.metric_summaries.map((metric) => <code key={metric.name}>{metricSummary(metric)}</code>)}</span> : <span className="muted">No completed metric samples</span> },
          { label: "traces", render: (cell) => <Link className="text-link" href={`/episodes/?cell_id=${encodeURIComponent(cell.cell_id)}`}>View traces</Link> },
        ]} />}
      </section>
      <section className="card section-card">
        <div className="section-heading"><h2>Launches</h2><p>A launch fills planned replications. Retried executions remain visible rather than replacing prior evidence.</p></div>
        {launches.length === 0 ? <p className="empty-state">No launches yet.</p> : <DataTable rows={launches} rowKey={(entry) => entry.launch.id} columns={[
          { label: "launch", render: (entry) => <Link className="table-link" href={`/launch/?id=${encodeURIComponent(entry.launch.id)}`}><strong className="mono">{entry.launch.id}</strong><span>Open diagnostics</span></Link> },
          { label: "mode", render: (entry) => <Chip>{entry.launch.mode}</Chip> },
          { label: "status", render: (entry) => <Chip tone={entry.launch.status === "COMPLETED" ? "good" : "warn"}>{entry.launch.status}</Chip> },
          { label: "replications", className: "mono", render: (entry) => `${entry.progress.completed}/${entry.progress.planned}` },
        ]} />}
      </section>
      <section className="card section-card">
        <div className="section-heading">
          <h2>Episodes</h2>
          <p>One row per logical replication, showing its latest physical result. Earlier physical runs remain available as trace evidence.</p>
        </div>
        {replicationGroups.length === 0 ? <p className="empty-state">No episodes recorded for this experiment yet.</p> : <DataTable rows={replicationGroups} rowKey={(group) => group.key} columns={[
          { label: "episode", render: (group) => <Link className="table-link" href={`/episode/?uid=${encodeURIComponent(group.latest.episode_uid)}`}><strong>{group.latest.episode_id ?? group.latest.episode_uid}</strong><span>{group.latest.environment_id ?? "unknown environment"}</span></Link> },
          { label: "cell", className: "mono", render: (group) => group.latest.cell_id ?? "-" },
          { label: "replication", className: "mono", render: (group) => replicationProgress(group.latest, plannedReplicationsByCell) },
          { label: "prior runs", render: (group) => <ReplicationHistory executions={group.priorExecutions} /> },
          { label: "item", className: "mono", render: (group) => group.latest.item_id ?? <span className="muted">-</span> },
          { label: "latest status", render: (group) => <Chip tone={EPISODE_TONE[group.latest.status] ?? "plain"}>{group.latest.status}</Chip> },
        ]} />}
      </section>
    </>
  );
}
