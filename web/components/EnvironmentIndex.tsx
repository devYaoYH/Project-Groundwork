"use client";

import Link from "next/link";
import { useEffect, useState } from "react";

import { Crumb } from "./Crumb";
import { EnvironmentSummary, listEnvironments } from "../lib/api";
import { environmentTitle } from "../lib/quick-play";

export function EnvironmentIndex() {
  const [environments, setEnvironments] = useState<EnvironmentSummary[]>([]);
  const [error, setError] = useState<string | null>(null);
  // "Still loading" and "nothing installed" are different answers, and only
  // this flag separates them; without it an empty list reads as a hang.
  const [loaded, setLoaded] = useState(false);

  useEffect(() => {
    listEnvironments()
      .then((next) => setEnvironments(next ?? []))
      .catch((reason: Error) => setError(reason.message))
      .finally(() => setLoaded(true));
  }, []);

  return (
    <>
      <Crumb items={[{ label: "environments" }]} />
      <header className="page-heading">
        <div>
          <h1>Environments</h1>
          <p>Choose an environment, pick your participants, and play one episode. Explore the details when you want to go deeper.</p>
        </div>
      </header>
      {error ? <p className="notice notice-error">{error}</p> : null}
      <section className="environment-grid" aria-label="Installed environments">
        {environments.length === 0 && !error ? (
          <p className="empty-state">
            {loaded ? "No environment releases are installed in this workspace." : "Loading registered environments..."}
          </p>
        ) : null}
        {environments.length > 0 ? (
          environments.map((environment, index) => <article className="card environment-card" key={environment.environment_id}>
            <div className="environment-card-meta"><span className="environment-number">{String(index + 1).padStart(2, "0")}</span><span>Release {environment.version}</span></div>
            <h2>{environmentTitle(environment.environment_id)}</h2>
            <p>{environment.blurb}</p>
            <div className="launch-actions">
              <Link className="button button-primary" href={`/environments/${environment.environment_id}/play/`}>Quick Play<span className="visually-hidden"> {environmentTitle(environment.environment_id)}</span></Link>
              <Link className="button" href={`/environments/${environment.environment_id}/`}>Details<span className="visually-hidden"> {environmentTitle(environment.environment_id)}</span></Link>
            </div>
          </article>)
        ) : null}
      </section>
    </>
  );
}
