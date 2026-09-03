"use client";

import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { useEffect, useState } from "react";

import { Chip } from "./Chip";
import { Crumb } from "./Crumb";
import { DataTable } from "./DataTable";
import { ExperimentDetail, getExperiment } from "../lib/api";

export function ExperimentDetailView() {
  const search = useSearchParams();
  const id = search.get("id") || "";
  const [detail, setDetail] = useState<ExperimentDetail | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!id) return;
    getExperiment(id).then(setDetail).catch((reason: Error) => setError(reason.message));
  }, [id]);

  if (!id) return <p className="notice notice-error">Choose an experiment from the library.</p>;
  if (error) return <p className="notice notice-error">{error}</p>;
  if (!detail) return <p className="empty-state">Loading experiment...</p>;
  const experiment = detail.experiment;
  const newestLaunch = detail.launches[0];
  const progress = newestLaunch?.progress;
  return (
    <>
      <Crumb items={[{ label: "experiments", href: "/experiments/" }, { label: experiment.name }]} />
      <header className="page-heading page-heading-split">
        <div><h1>{experiment.name}</h1><p>Release <code>{experiment.release_id}</code> · {experiment.locked_at ? `locked ${new Date(experiment.locked_at).toLocaleString()}` : "draft — live execution is locked"}</p></div>
        {experiment.design_text ? <Link className="button button-primary" href={`/design/?id=${encodeURIComponent(experiment.id)}`}>Open design</Link> : null}
      </header>
      <section className="metric-grid">
        <div className="metric"><span>cells</span><strong>{detail.cells.length}</strong></div>
        <div className="metric"><span>episodes planned</span><strong>{detail.cells.reduce((total, cell) => total + cell.episodes_planned, 0)}</strong></div>
        <div className="metric"><span>latest launch</span><strong>{progress ? `${progress.completed}/${progress.planned}` : "-"}</strong></div>
      </section>
      <section className="card section-card">
        <div className="section-heading"><h2>Cells</h2><p>Levels are fixed at preregistration; randomized item attributes remain on each episode record.</p></div>
        <DataTable rows={detail.cells} rowKey={(cell) => cell.cell_id} columns={[
          { label: "cell", className: "mono", render: (cell) => cell.cell_id },
          { label: "levels", className: "mono muted", render: (cell) => JSON.stringify(cell.levels) },
          { label: "episodes", className: "mono", render: (cell) => cell.episodes_planned },
          { label: "progress", className: "mono", render: (cell) => {
            const matching = progress?.by_cell.find((entry) => entry.cell_id === cell.cell_id);
            return matching ? `${matching.completed}/${matching.planned}` : "-";
          } },
        ]} />
      </section>
      <section className="card section-card">
        <div className="section-heading"><h2>Launches</h2><p>A launch fills planned episodes. Retried attempts remain visible rather than replacing prior evidence.</p></div>
        {detail.launches.length === 0 ? <p className="empty-state">No launches yet.</p> : <DataTable rows={detail.launches} rowKey={(entry) => entry.launch.id} columns={[
          { label: "launch", className: "mono", render: (entry) => entry.launch.id },
          { label: "mode", render: (entry) => <Chip>{entry.launch.mode}</Chip> },
          { label: "status", render: (entry) => <Chip tone={entry.launch.status === "COMPLETED" ? "good" : "warn"}>{entry.launch.status}</Chip> },
          { label: "episodes", className: "mono", render: (entry) => `${entry.progress.completed}/${entry.progress.planned}` },
        ]} />}
      </section>
    </>
  );
}
