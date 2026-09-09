import type { EpisodeFilters } from "./api.ts";

export type EpisodeQueryIssue = { token: string; message: string };

export type EpisodeQuery = {
  raw: string;
  filters: EpisodeFilters;
  tokens: { prefix: "cell" | "experiment" | "environment" | "status"; value: string }[];
  terms: string[];
  issues: EpisodeQueryIssue[];
  environments: string[];
  statuses: string[];
};

type EpisodeQueryState = Pick<EpisodeQuery, "raw" | "environments" | "statuses">;

const PREFIXES = new Set(["cell", "experiment", "environment", "status"]);

function unique(values: string[]): string[] {
  return [...new Set(values.filter(Boolean))];
}

export function parseEpisodeQuery(
  raw: string,
  facets: { environments?: string[]; statuses?: string[] } = {},
): EpisodeQuery {
  const tokens: EpisodeQuery["tokens"] = [];
  const terms: string[] = [];
  const issues: EpisodeQueryIssue[] = [];
  const environments = [...(facets.environments ?? [])];
  const statuses = [...(facets.statuses ?? [])];
  let cellId: string | undefined;
  let experimentId: string | undefined;

  for (const token of raw.trim().split(/\s+/).filter(Boolean)) {
    const colon = token.indexOf(":");
    if (colon < 0) {
      terms.push(token);
      continue;
    }
    const prefix = token.slice(0, colon).toLowerCase();
    const value = token.slice(colon + 1);
    if (!PREFIXES.has(prefix)) {
      issues.push({ token, message: `Unknown filter prefix ${prefix}.` });
      continue;
    }
    if (!value) {
      issues.push({ token, message: `${prefix}: needs an exact value.` });
      continue;
    }
    tokens.push({ prefix: prefix as EpisodeQuery["tokens"][number]["prefix"], value });
    if (prefix === "cell") {
      if (cellId && cellId !== value) issues.push({ token, message: "Use one cell: filter." });
      cellId = value;
    } else if (prefix === "experiment") {
      if (experimentId && experimentId !== value) issues.push({ token, message: "Use one experiment: filter." });
      experimentId = value;
    } else if (prefix === "environment") {
      environments.push(value);
    } else {
      statuses.push(value);
    }
  }

  const filters: EpisodeFilters = {};
  if (terms.length) filters.q = terms.join(" ");
  if (cellId) filters.cell_id = cellId;
  if (experimentId) filters.experiment_id = experimentId;
  const selectedEnvironments = unique(environments);
  const selectedStatuses = unique(statuses);
  if (selectedEnvironments.length) filters.environment_id = selectedEnvironments;
  if (selectedStatuses.length) filters.status = selectedStatuses;
  return { raw, filters, tokens, terms, issues, environments: selectedEnvironments, statuses: selectedStatuses };
}

export function serializeEpisodeQuery(state: EpisodeQueryState): string {
  const query = new URLSearchParams();
  if (state.raw.trim()) query.set("q", state.raw.trim());
  for (const environment of unique(state.environments)) query.append("environment_id", environment);
  for (const status of unique(state.statuses)) query.append("status", status);
  return query.toString();
}

export function episodeQueryFromSearch(search: URLSearchParams): EpisodeQueryState {
  const raw = search.get("q") ?? "";
  const legacy = [
    search.get("experiment_id") ? `experiment:${search.get("experiment_id")}` : "",
    search.get("cell_id") ? `cell:${search.get("cell_id")}` : "",
  ].filter(Boolean);
  return {
    raw: [raw, ...legacy].filter(Boolean).join(" "),
    environments: search.getAll("environment_id"),
    statuses: search.getAll("status"),
  };
}
