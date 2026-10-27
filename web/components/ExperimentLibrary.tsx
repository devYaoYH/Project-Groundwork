"use client";

import Link from "next/link";
import { useEffect, useState } from "react";

import { Chip } from "./Chip";
import { Crumb } from "./Crumb";
import { DataTable } from "./DataTable";
import {
  EnvironmentSummary,
  Experiment,
  createDesignExperiment,
  getEnvironment,
  listEnvironments,
  listExperiments,
} from "../lib/api";
import { initialDesign } from "../lib/design-template";

export function ExperimentLibrary() {
  const [experiments, setExperiments] = useState<Experiment[]>([]);
  const [environments, setEnvironments] = useState<EnvironmentSummary[]>([]);
  const [name, setName] = useState("Untitled experiment");
  const [releaseId, setReleaseId] = useState("");
  const [creating, setCreating] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    Promise.all([listExperiments(), listEnvironments()])
      .then(([nextExperiments, nextEnvironments]) => {
        setExperiments(nextExperiments);
        setEnvironments(nextEnvironments);
        setReleaseId((current) => current || nextEnvironments[0]?.release_id || "");
      })
      .catch((reason: Error) => setError(reason.message));
  }, []);

  async function create() {
    const environment = environments.find((entry) => entry.release_id === releaseId);
    if (!environment || !name.trim()) return;
    setCreating(true);
    setError(null);
    try {
      const detail = await getEnvironment(environment.environment_id);
      const experiment = await createDesignExperiment(name.trim(), releaseId, initialDesign(detail));
      window.location.assign(`/design/?id=${encodeURIComponent(experiment.id)}`);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Unable to create experiment");
      setCreating(false);
    }
  }

  return (
    <>
      <Crumb items={[{ label: "experiments" }]} />
      <header className="page-heading page-heading-split">
        <div>
          <h1>Your experiments</h1>
          <p>Each study pins a release and retains the authored design that produced its fixed episode plan.</p>
        </div>
      </header>
      {error ? <p className="notice notice-error">{error}</p> : null}
      <section className="card experiment-create">
        <label><span>Experiment name</span><input value={name} onChange={(event) => setName(event.target.value)} /></label>
        <label><span>Release</span><select value={releaseId} onChange={(event) => setReleaseId(event.target.value)}>{environments.map((environment) => <option key={environment.release_id} value={environment.release_id}>{environment.environment_id} {environment.version}</option>)}</select></label>
        <button className="button button-primary" onClick={create} disabled={creating || !releaseId || !name.trim()}>{creating ? "Creating..." : "New experiment"}</button>
      </section>
      <section className="card">
        {experiments.length === 0 ? <p className="empty-state">Create a design to begin a preregistered experiment.</p> : <DataTable
          rows={experiments}
          rowKey={(experiment) => experiment.id}
          columns={[
            { label: "experiment", render: (experiment) => <Link className="table-link" href={`/experiment/?id=${encodeURIComponent(experiment.id)}`}><strong>{experiment.name}</strong><span>{experiment.id}</span></Link> },
            { label: "release", className: "mono", render: (experiment) => experiment.release_id },
            { label: "design", render: (experiment) => experiment.design_text ? <Chip tone={experiment.locked_at ? "good" : "warn"}>{experiment.locked_at ? "locked" : "draft"}</Chip> : <Chip>direct YAML</Chip> },
            { label: "created", className: "muted", render: (experiment) => new Date(experiment.created_at).toLocaleString() },
          ]}
        />}
      </section>
    </>
  );
}
