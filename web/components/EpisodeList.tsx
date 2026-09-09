"use client";

import Link from "next/link";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { useEffect, useMemo, useRef, useState } from "react";

import { Chip } from "./Chip";
import { Crumb } from "./Crumb";
import { DataTable } from "./DataTable";
import { ReplicationHistory } from "./ReplicationHistory";
import { EpisodeFacet, EpisodeSummary, getExperiment, listEpisodes } from "../lib/api";
import { episodeQueryFromSearch, parseEpisodeQuery, serializeEpisodeQuery } from "../lib/episode-query";
import { groupReplicationExecutions, replicationProgress } from "../lib/replications";

const TONE: Record<string, "plain" | "good" | "warn"> = {
  COMPLETED: "good",
  PARTIAL: "warn",
  STOPPED: "warn",
};

function headline(metrics: Record<string, unknown>) {
  const scalars = Object.entries(metrics).filter(
    ([, value]) => typeof value === "number" || typeof value === "boolean",
  );
  if (scalars.length === 0) return <span className="muted">-</span>;
  return <code>{scalars.slice(0, 3).map(([name, value]) => `${name} ${String(value)}`).join("  ·  ")}</code>;
}

function facetOptions(facets: EpisodeFacet[], selected: string[]): EpisodeFacet[] {
  const counts = new Map(facets.map((facet) => [facet.value, facet.count]));
  for (const value of selected) if (!counts.has(value)) counts.set(value, 0);
  return [...counts.entries()]
    .map(([value, count]) => ({ value, count }))
    .sort((left, right) => left.value.localeCompare(right.value));
}

export function EpisodeList() {
  const search = useSearchParams();
  const router = useRouter();
  const pathname = usePathname();
  const [queryState, setQueryState] = useState(() => episodeQueryFromSearch(search));
  const [episodes, setEpisodes] = useState<EpisodeSummary[]>([]);
  const [cursor, setCursor] = useState<string | null>(null);
  const [facets, setFacets] = useState({ environments: [] as EpisodeFacet[], statuses: [] as EpisodeFacet[] });
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [plannedReplicationsByCell, setPlannedReplicationsByCell] = useState<Record<string, number>>({});
  const parsed = useMemo(
    () => parseEpisodeQuery(queryState.raw, { environments: queryState.environments, statuses: queryState.statuses }),
    [queryState],
  );
  const queryKey = JSON.stringify(parsed.filters);
  const queryKeyRef = useRef(queryKey);
  queryKeyRef.current = queryKey;

  useEffect(() => {
    const next = episodeQueryFromSearch(search);
    setQueryState((current) => JSON.stringify(current) === JSON.stringify(next) ? current : next);
    const canonical = serializeEpisodeQuery(next);
    if (canonical !== search.toString()) router.replace(canonical ? `${pathname}?${canonical}` : pathname, { scroll: false });
  }, [pathname, router, search]);

  useEffect(() => {
    let current = true;
    setLoading(true);
    listEpisodes(parsed.filters)
      .then((page) => {
        if (!current) return;
        setEpisodes(page.episodes ?? []);
        setCursor(page.next_cursor ?? null);
        setFacets(page.facets ?? { environments: [], statuses: [] });
        setError(null);
      })
      .catch((reason: Error) => { if (current) setError(reason.message); })
      .finally(() => { if (current) setLoading(false); });
    return () => { current = false; };
  }, [parsed.filters, queryKey]);

  useEffect(() => {
    const experimentIds = [...new Set(episodes.map((episode) => episode.experiment_id).filter((id): id is string => Boolean(id)))];
    if (experimentIds.length === 0) return;
    let current = true;
    Promise.all(experimentIds.map(getExperiment))
      .then((details) => {
        if (!current) return;
        setPlannedReplicationsByCell((existing) => ({
          ...existing,
          ...Object.fromEntries(details.flatMap((detail) => (detail.cells ?? []).map((cell) => [cell.cell_id, cell.episodes_planned] as const))),
        }));
      })
      .catch(() => undefined);
    return () => { current = false; };
  }, [episodes]);

  function updateQuery(next: typeof queryState) {
    setQueryState(next);
    const serialized = serializeEpisodeQuery(next);
    router.replace(serialized ? `${pathname}?${serialized}` : pathname, { scroll: false });
  }

  function toggleFacet(kind: "environments" | "statuses", value: string) {
    const values = queryState[kind];
    updateQuery({
      ...queryState,
      [kind]: values.includes(value) ? values.filter((entry) => entry !== value) : [...values, value],
    });
  }

  async function loadMore() {
    if (!cursor) return;
    const requestKey = queryKey;
    const page = await listEpisodes(parsed.filters, cursor);
    if (queryKeyRef.current !== requestKey) return;
    setEpisodes((current) => [...current, ...(page.episodes ?? [])]);
    setCursor(page.next_cursor ?? null);
  }

  return (
    <>
      <Crumb items={[{ label: "episodes" }]} />
      <header className="page-heading"><div><h1>Episodes</h1><p>Search episode names, narrow with exact field tokens, and retain the view in a shareable URL. Each row is one logical replication with its latest physical result.</p></div></header>

      <section className="episode-search card" aria-label="Episode search">
        <label className="episode-query"><span>Search episodes</span><input value={queryState.raw} placeholder="testing fork cell:cell-abc status:COMPLETED" onChange={(event) => updateQuery({ ...queryState, raw: event.target.value })} /></label>
        <p className="episode-query-help">Plain terms match episode-name tokens. Exact filters: <code>cell:</code>, <code>experiment:</code>, <code>environment:</code>, <code>status:</code>.</p>
        {parsed.tokens.length ? <p className="applied-tokens">Applied: {parsed.tokens.map((token) => <Chip key={`${token.prefix}:${token.value}`}>{token.prefix}: {token.value}</Chip>)}</p> : null}
        {parsed.issues.map((issue) => <p className="notice notice-error" key={`${issue.token}:${issue.message}`}>{issue.message}</p>)}
        <div className="facet-groups">
          <fieldset><legend>Environment</legend>{facetOptions(facets.environments, parsed.environments).map((facet) => <label key={facet.value}><input type="checkbox" checked={parsed.environments.includes(facet.value)} onChange={() => toggleFacet("environments", facet.value)} /> <code>{facet.value}</code> <span>({facet.count})</span></label>)}</fieldset>
          <fieldset><legend>Status</legend>{facetOptions(facets.statuses, parsed.statuses).map((facet) => <label key={facet.value}><input type="checkbox" checked={parsed.statuses.includes(facet.value)} onChange={() => toggleFacet("statuses", facet.value)} /> <code>{facet.value}</code> <span>({facet.count})</span></label>)}</fieldset>
        </div>
      </section>

      {error ? <p className="notice notice-error">{error}</p> : null}
      <section className="card">
        {loading && episodes.length === 0 ? <p className="empty-state">Loading episodes...</p> : null}
        {!loading && episodes.length === 0 && !error ? <p className="empty-state">No episodes match this view yet.</p> : null}
        {episodes.length > 0 ? <DataTable rows={groupReplicationExecutions(episodes)} rowKey={(group) => group.key} columns={[
          { label: "episode", render: (group) => <Link className="table-link" href={`/episode/?uid=${encodeURIComponent(group.latest.episode_uid)}`}><strong>{group.latest.episode_id ?? group.latest.episode_uid}</strong><span>{group.latest.environment_id ?? "unknown environment"}</span></Link> },
          { label: "cell", className: "mono", render: (group) => group.latest.cell_id ?? "-" },
          { label: "replication", className: "mono", render: (group) => replicationProgress(group.latest, plannedReplicationsByCell) },
          { label: "prior runs", render: (group) => <ReplicationHistory executions={group.priorExecutions} /> },
          { label: "seed", className: "mono", render: (group) => group.latest.seed === null ? <span className="muted">-</span> : group.latest.seed },
          { label: "item", className: "mono", render: (group) => group.latest.item_id ?? <span className="muted">-</span> },
          { label: "latest status", render: (group) => <Chip tone={TONE[group.latest.status] ?? "plain"}>{group.latest.status}</Chip> },
          { label: "measures", render: (group) => headline(group.latest.metrics) },
        ]} /> : null}
      </section>
      {cursor ? <button className="button" onClick={loadMore}>Load more episodes</button> : null}
    </>
  );
}
