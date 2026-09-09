"use client";

import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { useCallback, useEffect, useState } from "react";

import { Chip } from "./Chip";
import { Crumb } from "./Crumb";
import { DataTable } from "./DataTable";
import { ReplicationHistory } from "./ReplicationHistory";
import { EpisodeFilters, EpisodeSummary, getExperiment, listEpisodes } from "../lib/api";
import { groupReplicationExecutions, replicationProgress } from "../lib/replications";

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
  const search = useSearchParams();
  const linkedCellId = search.get("cell_id") || "";
  const [filters, setFilters] = useState<EpisodeFilters>(() => (
    linkedCellId ? { cell_id: linkedCellId } : {}
  ));
  const [episodes, setEpisodes] = useState<EpisodeSummary[]>([]);
  const [cursor, setCursor] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [plannedReplicationsByCell, setPlannedReplicationsByCell] = useState<Record<string, number>>({});

  useEffect(() => {
    if (!linkedCellId) return;
    setFilters((current) => current.cell_id === linkedCellId ? current : {
      ...current,
      cell_id: linkedCellId,
    });
  }, [linkedCellId]);

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

  useEffect(() => {
    const experimentIds = [...new Set(
      episodes.map((episode) => episode.experiment_id).filter((id): id is string => Boolean(id)),
    )];
    if (experimentIds.length === 0) return;
    let current = true;
    Promise.all(experimentIds.map(getExperiment))
      .then((details) => {
        if (!current) return;
        const nextPlans = Object.fromEntries(details.flatMap((detail) => (
          (detail.cells ?? []).map((cell) => [cell.cell_id, cell.episodes_planned] as const)
        )));
        setPlannedReplicationsByCell((existing) => ({ ...existing, ...nextPlans }));
      })
      // Trace results remain useful even when an older experiment record is no
      // longer available to provide its locked plan count.
      .catch(() => undefined);
    return () => { current = false; };
  }, [episodes]);

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
            One row per logical replication, showing its latest physical result. Earlier physical runs remain
            accessible evidence. Cell, seed and replication are promoted from the provenance block stamped when
            the episode was expanded, so this list is a query rather than a scan.
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
            rows={groupReplicationExecutions(episodes)}
            rowKey={(group) => group.key}
            columns={[
              {
                label: "episode",
                render: (group) => (
                  <Link className="table-link" href={`/episode/?uid=${encodeURIComponent(group.latest.episode_uid)}`}>
                    <strong>{group.latest.episode_id ?? group.latest.episode_uid}</strong>
                    <span>{group.latest.environment_id ?? "unknown environment"}</span>
                  </Link>
                ),
              },
              { label: "cell", className: "mono", render: (group) => group.latest.cell_id ?? "-" },
              {
                label: "replication",
                className: "mono",
                render: (group) => replicationProgress(group.latest, plannedReplicationsByCell),
              },
              { label: "prior runs", render: (group) => <ReplicationHistory executions={group.priorExecutions} /> },
              {
                label: "seed",
                className: "mono",
                render: (group) =>
                  group.latest.seed === null ? <span className="muted">-</span> : group.latest.seed,
              },
              {
                label: "item",
                className: "mono",
                render: (group) => group.latest.item_id ?? <span className="muted">-</span>,
              },
              {
                label: "latest status",
                render: (group) => (
                  <Chip tone={TONE[group.latest.status] ?? "plain"}>{group.latest.status}</Chip>
                ),
              },
              { label: "measures", render: (group) => headline(group.latest.metrics) },
            ]}
          />
        ) : null}
      </section>
      {cursor ? <button className="button" onClick={loadMore}>Load more episodes</button> : null}
    </>
  );
}
