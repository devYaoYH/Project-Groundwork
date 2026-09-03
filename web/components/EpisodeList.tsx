"use client";

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";

import { Chip } from "./Chip";
import { Crumb } from "./Crumb";
import { DataTable } from "./DataTable";
import { EpisodeFilters, EpisodeSummary, listEpisodes } from "../lib/api";

// PARTIAL is a trace recovered from an interrupted episode's event log. It is
// evidence for inspection and retry, so it is shown — and marked as not being
// a result.
const TONE: Record<string, "plain" | "good" | "warn"> = {
  COMPLETED: "good",
  PARTIAL: "warn",
  STOPPED: "warn",
};

const FILTER_FIELDS: { key: keyof EpisodeFilters; label: string; placeholder: string }[] = [
  { key: "experiment_name", label: "experiment", placeholder: "all experiments" },
  { key: "environment_id", label: "environment", placeholder: "all environments" },
  { key: "cell_id", label: "cell", placeholder: "all cells" },
  { key: "status", label: "status", placeholder: "any status" },
];

function headline(metrics: Record<string, unknown>) {
  const scalars = Object.entries(metrics).filter(
    ([, value]) => typeof value === "number" || typeof value === "boolean",
  );
  if (scalars.length === 0) return <span className="muted">-</span>;
  return (
    <code>
      {scalars.slice(0, 3).map(([name, value]) => `${name} ${String(value)}`).join("  ·  ")}
    </code>
  );
}

export function EpisodeList() {
  const [filters, setFilters] = useState<EpisodeFilters>({});
  const [episodes, setEpisodes] = useState<EpisodeSummary[]>([]);
  const [cursor, setCursor] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  const load = useCallback(() => {
    setLoading(true);
    listEpisodes(filters)
      .then((page) => {
        // An empty corpus is the common first view, and a server that answers
        // with no `episodes` key at all must read as empty rather than crash.
        setEpisodes(page.episodes ?? []);
        setCursor(page.next_cursor ?? null);
        setError(null);
      })
      .catch((reason: Error) => setError(reason.message))
      .finally(() => setLoading(false));
  }, [filters]);

  useEffect(load, [load]);

  async function loadMore() {
    if (!cursor) return;
    const page = await listEpisodes(filters, cursor);
    setEpisodes((current) => [...current, ...(page.episodes ?? [])]);
    setCursor(page.next_cursor ?? null);
  }

  return (
    <>
      <Crumb items={[{ label: "episodes" }]} />
      <header className="page-heading">
        <div>
          <h1>Episodes</h1>
          <p>
            One row per attempt. Cell, seed and attempt are promoted from the provenance block
            stamped when the episode was expanded, so this list is a query rather than a scan.
          </p>
        </div>
      </header>

      <form className="filter-bar card" onSubmit={(event) => event.preventDefault()}>
        {FILTER_FIELDS.map((field) => (
          <label key={field.key}>
            <span>{field.label}</span>
            <input
              value={filters[field.key] ?? ""}
              placeholder={field.placeholder}
              onChange={(event) =>
                setFilters((current) => ({ ...current, [field.key]: event.target.value }))
              }
            />
          </label>
        ))}
      </form>

      {error ? <p className="notice notice-error">{error}</p> : null}
      <section className="card">
        {loading && episodes.length === 0 ? <p className="empty-state">Loading episodes...</p> : null}
        {!loading && episodes.length === 0 && !error ? (
          <p className="empty-state">No episodes match these filters yet.</p>
        ) : null}
        {episodes.length > 0 ? (
          <DataTable
            rows={episodes}
            rowKey={(episode) => episode.episode_uid}
            columns={[
              {
                label: "episode",
                render: (episode) => (
                  <Link className="table-link" href={`/episode/?uid=${encodeURIComponent(episode.episode_uid)}`}>
                    <strong>{episode.episode_id ?? episode.episode_uid}</strong>
                    <span>{episode.environment_id ?? "unknown environment"}</span>
                  </Link>
                ),
              },
              { label: "cell", className: "mono", render: (episode) => episode.cell_id ?? "-" },
              {
                label: "attempt",
                className: "mono",
                render: (episode) => episode.attempt,
              },
              {
                label: "seed",
                className: "mono",
                render: (episode) =>
                  episode.seed === null ? <span className="muted">-</span> : episode.seed,
              },
              {
                label: "item",
                className: "mono",
                render: (episode) => episode.item_id ?? <span className="muted">-</span>,
              },
              {
                label: "status",
                render: (episode) => (
                  <Chip tone={TONE[episode.status] ?? "plain"}>{episode.status}</Chip>
                ),
              },
              { label: "measures", render: (episode) => headline(episode.metrics) },
            ]}
          />
        ) : null}
      </section>
      {cursor ? <button className="button" onClick={loadMore}>Load more episodes</button> : null}
    </>
  );
}
