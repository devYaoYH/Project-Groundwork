"use client";

import Link from "next/link";
import { useEffect, useState } from "react";

import { Chip } from "./Chip";
import { Crumb } from "./Crumb";
import { DataTable } from "./DataTable";
import { EnvironmentSummary, listEnvironments } from "../lib/api";

export function EnvironmentIndex() {
  const [environments, setEnvironments] = useState<EnvironmentSummary[]>([]);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    listEnvironments().then(setEnvironments).catch((reason: Error) => setError(reason.message));
  }, []);

  return (
    <>
      <Crumb items={[{ label: "environments" }]} />
      <header className="page-heading">
        <div>
          <h1>Environments</h1>
          <p>Shared, versioned, read-only. What each release declares here is what the design screen will let you put on an axis.</p>
        </div>
      </header>
      {error ? <p className="notice notice-error">{error}</p> : null}
      <section className="card">
        {environments.length === 0 && !error ? <p className="empty-state">Loading registered environments...</p> : null}
        {environments.length > 0 ? (
          <DataTable
            rows={environments}
            rowKey={(environment) => environment.environment_id}
            columns={[
              {
                label: "environment",
                render: (environment) => (
                  <Link className="table-link" href={`/environments/${environment.environment_id}/`}>
                    <strong>{environment.environment_id}</strong>
                    <span>{environment.blurb}</span>
                  </Link>
                ),
              },
              { label: "release", className: "mono", render: (environment) => environment.version },
              {
                label: "used by",
                render: (environment) => environment.experiment_count ? <Chip>{environment.experiment_count} experiments</Chip> : <span className="muted">-</span>,
              },
              {
                label: "source",
                className: "align-right",
                render: (environment) => <a className="source-link" href={environment.source_url} target="_blank" rel="noreferrer">source</a>,
              },
            ]}
          />
        ) : null}
      </section>
    </>
  );
}
