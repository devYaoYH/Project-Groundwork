export type Level = {
  value: unknown;
  count: number;
};

export type Parameter = {
  name: string;
  type: "continuous" | "integer" | "categorical" | "boolean";
  domain: unknown[] | null;
  fixed: boolean;
  // Where the value comes from. A design parameter is set by the researcher;
  // an item parameter is frozen in the bank, so a factor over it selects rows.
  source: "design" | "item";
  item_key: string | null;
  // Projected from the pinned bank for item parameters, null for design ones.
  levels: Level[] | null;
  // One distinct level per item: the column restates the item's identity.
  identity_grained: boolean;
};

export type Role = {
  id: string;
  count: number;
  accepts: string[];
  description: string;
};

export type Measure = {
  name: string;
  producer: "environment" | "derived";
  grain: "episode" | "sequence";
  index_label: string | null;
  unit: "joint" | "participant";
  direction: "maximize" | "minimize" | "neutral";
};

export type EnvironmentSummary = {
  environment_id: string;
  blurb: string;
  version: string;
  release_id: string;
  source_url: string;
  experiment_count: number;
};

export type EnvironmentDetail = {
  environment_id: string;
  release: {
    release_id: string;
    version: string;
    declaration_sha256: string;
    item_bank_sha256: string;
    oracle_version: string | null;
  };
  parameters: Parameter[];
  roles: Role[];
  measures: Measure[];
  item_policy: {
    mode: "enumerate" | "sample";
    bank_path: string;
    item_bank_sha256: string;
    item_count: number;
  };
  declaration_yaml: string;
};

export type Item = {
  item_id: string;
  params: Record<string, unknown>;
  oracle_result: unknown | null;
};

export type ItemPage = {
  items: Item[];
  next_cursor: string | null;
  item_bank_sha256: string;
};

export type OracleResult = {
  item_id: string;
  oracle_version: string;
  result: unknown;
};

// One row of the episodes fact table, at one-attempt grain. Every identity
// field here is promoted from the provenance block the compiler stamped, so
// the list is a SQL query rather than a scan over rehydrated traces.
export type EpisodeSummary = {
  episode_uid: string;
  environment_id: string | null;
  experiment_name: string | null;
  episode_id: string | null;
  cell_id: string | null;
  episode_idx: number | null;
  experiment_id: string | null;
  release_id: string | null;
  item_id: string | null;
  attempt: number;
  execution?: number;
  shard_index?: number | null;
  seed: number | null;
  // COMPLETED | STOPPED | PARTIAL. PARTIAL is a trace recovered from an
  // interrupted episode's event log: evidence, not a result.
  status: string;
  run_mode: "live" | "smoke" | "dry_run";
  provenance_grade?: "verified" | "unverified";
  image_digest?: string | null;
  started_at: string | null;
  ended_at: string | null;
  stopped: boolean;
  created_at: string | null;
  metrics: Record<string, unknown>;
};

export type EpisodeFilters = {
  q?: string;
  experiment_id?: string;
  experiment_name?: string;
  environment_id?: string[];
  cell_id?: string;
  status?: string[];
  run_mode?: string[];
};

export type EpisodeFacet = { value: string; count: number };

// One emitted event. `type` is environment-defined and `data` is a free dict,
// so nothing beyond those two may be assumed present.
export type TraceEvent = {
  type?: string | null;
  timestamp?: string | null;
  data?: Record<string, unknown> | null;
};

// The persisted record of one attempt, as the store holds it.
export type EpisodeTrace = {
  episode_uid: string;
  config: Record<string, unknown> | null;
  events: TraceEvent[] | null;
  final_state: Record<string, unknown> | null;
  metrics: Record<string, unknown> | null;
  release: Record<string, unknown> | null;
  episode: Record<string, unknown> | null;
  observability: Record<string, unknown> | null;
  started_at: string | null;
  ended_at: string | null;
  stopped: boolean;
};

export type Lane = {
  participant_id: string;
  kind: string;
  binding: string | null;
  role?: string | null;
};

// The trace, plus the two read-time projections the browser cannot derive:
// who the pinned roster was, and what the release calls its inner index. They
// ride alongside the record rather than inside it.
export type EpisodeDetail = {
  episode: EpisodeTrace;
  run_mode: "live" | "smoke" | "dry_run";
  provenance_grade: "verified" | "unverified";
  lanes: Lane[] | null;
  index_label: string | null;
  cursor_max: number;
};

export type ArtifactPage = {
  episode_uid: string;
  artifacts: { kind: string; version: string; payload: unknown; metadata: unknown }[] | null;
};

export type EpisodePage = {
  episodes: EpisodeSummary[];
  next_cursor: string | null;
  filters: Record<string, string | string[]>;
  facets: { environments: EpisodeFacet[]; statuses: EpisodeFacet[]; run_modes: EpisodeFacet[] };
};

export type Experiment = {
  id: string;
  name: string;
  environment_id: string;
  release_id: string;
  yaml_path: string;
  config_sha256: string;
  created_at: string;
  design_text: string | null;
  design_sha256: string | null;
  locked_at: string | null;
  forked_from: string | null;
};

export type MetricSummary = {
  name: string;
  kind: "number" | "boolean";
  n: number;
  mean?: number;
  min?: number;
  max?: number;
  stddev?: number;
  true_count?: number;
  false_count?: number;
};

export type CellEvidence = {
  cell_id: string;
  levels: Record<string, unknown>;
  planned_replicas: number;
  completed_replicas: number;
  status_counts: Record<string, number>;
  metric_summaries: MetricSummary[];
};

export type AgentBinding = {
  name: string;
  description: string;
  type: string;
  model: string | null;
  api_format: string | null;
  api_base: string | null;
  credential: string | null;
  credential_present: boolean | null;
};

export type AgentPool = {
  agents: AgentBinding[];
  sources: string[];
};

export type CompiledPlan = {
  cells: { cell_id: string; levels: Record<string, unknown>; episodes_planned: number }[];
  episodes_planned: number;
  preview_episode_config: Record<string, unknown> | null;
};

export type DesignValidation = {
  valid: boolean;
  errors: { path: string; message: string }[];
  plan: CompiledPlan | null;
};

export type Launch = {
  id: string;
  experiment_id: string;
  status: string;
  max_parallelism: number;
  trace_database: string;
  mode: "live" | "smoke" | "dry_run";
  provenance_grade: "verified" | "unverified";
  image_digest: string | null;
  execution_path: string | null;
  shard_index: number | null;
  shard_count: number | null;
  created_at: string;
  started_at: string | null;
  ended_at: string | null;
  error: string | null;
};

export type LaunchAttempt = {
  id: string;
  launch_id: string;
  episode_id: string;
  cell_id: string;
  episode_idx: number;
  attempt: number;
  execution: number | null;
  status: string;
  episode_uid: string | null;
  episode_uri: string | null;
  error: string | null;
  started_at: string | null;
  ended_at: string | null;
  redis_stream: string;
};

// Every launch event, not only the parsed stdout lines this used to carry.
// The worker's output belongs to its platform; what a researcher reopens is
// the control plane's own record of what it did and what it then observed.
export type LaunchLog = {
  id: number;
  kind: string;
  payload: Record<string, unknown> & { line?: string };
  created_at: string;
};

export type ExperimentDetail = {
  experiment: Experiment;
  cells: CompiledPlan["cells"];
  cell_evidence: CellEvidence[];
  roster: { participant_id: string; kind: string; binding: string | null; role: string | null; config_sha256: string }[];
  launches: { launch: Launch; progress: { planned: number; completed: number; failed: number; by_cell: { cell_id: string; planned: number; completed: number; failed: number }[] } }[];
};

export type LaunchDetail = {
  launch: Launch;
  attempts: LaunchAttempt[];
  progress: ExperimentDetail["launches"][number]["progress"];
  runner_logs: LaunchLog[];
};

const baseUrl = process.env.NEXT_PUBLIC_API_BASE ?? "";

function environmentPath(id: string): string {
  const normalized = typeof id === "string" ? id.trim() : "";
  if (!normalized || normalized === "undefined") {
    throw new Error("A concrete environment id is required");
  }
  return encodeURIComponent(normalized);
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${baseUrl}${path}`, {
    ...init,
    headers: { "Content-Type": "application/json", ...init?.headers },
  });
  if (!response.ok) {
    const payload = await response.json().catch(() => ({}));
    throw new Error(payload.error ?? `Request failed (${response.status})`);
  }
  return response.json() as Promise<T>;
}

export async function listEnvironments(): Promise<EnvironmentSummary[]> {
  return (await request<{ environments: EnvironmentSummary[] }>("/api/environments")).environments;
}

export function getEnvironment(id: string): Promise<EnvironmentDetail> {
  return request<EnvironmentDetail>(`/api/environments/${environmentPath(id)}`);
}

export function getItems(id: string, cursor?: string): Promise<ItemPage> {
  const query = new URLSearchParams({ limit: "100" });
  if (cursor) query.set("cursor", cursor);
  return request<ItemPage>(`/api/environments/${environmentPath(id)}/items?${query}`);
}

export function listEpisodes(filters: EpisodeFilters, cursor?: string): Promise<EpisodePage> {
  const query = new URLSearchParams({ limit: "50" });
  for (const [key, value] of Object.entries(filters)) {
    if (Array.isArray(value)) {
      for (const entry of value) if (entry) query.append(key, entry);
    } else if (value) {
      query.set(key, value);
    }
  }
  if (cursor) query.set("cursor", cursor);
  return request<EpisodePage>(`/api/episodes?${query}`);
}

export function getEpisode(episodeUid: string): Promise<EpisodeDetail> {
  const wanted = typeof episodeUid === "string" ? episodeUid.trim() : "";
  if (!wanted || wanted === "undefined") {
    throw new Error("A concrete episode uid is required");
  }
  return request<EpisodeDetail>(`/api/episodes/${encodeURIComponent(wanted)}`);
}

export function getEpisodeArtifacts(episodeUid: string): Promise<ArtifactPage> {
  return request<ArtifactPage>(`/api/episodes/${encodeURIComponent(episodeUid)}/artifacts`);
}

export function runOracle(id: string, itemId: string): Promise<OracleResult> {
  return request<OracleResult>(`/api/environments/${environmentPath(id)}/oracle`, {
    method: "POST",
    body: JSON.stringify({ item_id: itemId }),
  });
}

export async function listExperiments(): Promise<Experiment[]> {
  return (await request<{ experiments: Experiment[] }>("/api/experiments")).experiments;
}

export function getAgentPool(): Promise<AgentPool> {
  return request<AgentPool>("/api/agent-pool");
}

export function getExperiment(id: string): Promise<ExperimentDetail> {
  return request<ExperimentDetail>(`/api/experiments/${encodeURIComponent(id)}`);
}

export function getLaunch(id: string): Promise<LaunchDetail> {
  return request<LaunchDetail>(`/api/launches/${encodeURIComponent(id)}`);
}

export function resumeAttempt(launchId: string, episodeId: string): Promise<Launch> {
  return request<Launch>(`/api/launches/${encodeURIComponent(launchId)}/resume`, {
    method: "POST",
    body: JSON.stringify({ episode_id: episodeId }),
  });
}

// The payload an ``attempt.progress`` event carries. Progress is derived by
// the control plane from durable evidence, so `durable_through` is a count of
// events that reached shared storage -- not a claim a worker made about
// itself.
export type AttemptProgress = {
  episode_id: string;
  attempt: number;
  status: string;
  durable_through: number;
  episode_uid: string | null;
};

// Every terminal launch event kind. A subscriber stops here and re-reads the
// launch rather than trying to assemble the final state from frames: the
// settled record is one fetch away and cannot disagree with itself.
export const TERMINAL_LAUNCH_EVENTS = new Set([
  "launch.settled", "launch.completed", "launch.failed", "launch.cancelled",
]);

/**
 * Subscribe to a launch's durable event log.
 *
 * Frames are deliberately unnamed server-side so a newly added event kind
 * reaches existing clients instead of being dropped by a name whitelist; the
 * kind travels in the payload, which is why this listens on `message` and
 * dispatches on `entry.kind`. The connection is short and bounded by design --
 * the browser reconnects on its own and resumes from the last id it saw -- so
 * there is nothing to keep alive here beyond closing on unmount.
 *
 * Returns a function that closes the stream. A browser without `EventSource`
 * gets a no-op, which leaves the caller's one-shot fetch as the whole view.
 */
export function subscribeLaunchEvents(
  id: string,
  onEvent: (entry: LaunchLog) => void,
): () => void {
  if (typeof EventSource === "undefined") return () => {};
  const source = new EventSource(`${baseUrl}/api/launches/${encodeURIComponent(id)}/events`);
  source.onmessage = (frame: MessageEvent) => {
    let entry: LaunchLog;
    try {
      entry = JSON.parse(frame.data as string) as LaunchLog;
    } catch {
      // A frame that will not parse is a frame nothing can be done about. The
      // live view degrades; the durable log it came from is still there.
      return;
    }
    if (entry && typeof entry.kind === "string") onEvent(entry);
  };
  return () => source.close();
}

export function validateDesign(releaseId: string, designText: string): Promise<DesignValidation> {
  return request<DesignValidation>("/api/designs/validate", {
    method: "POST",
    body: JSON.stringify({ release_id: releaseId, design_text: designText }),
  });
}

export function createDesignExperiment(name: string, releaseId: string, designText: string): Promise<Experiment> {
  return request<Experiment>("/api/experiments", {
    method: "POST",
    body: JSON.stringify({ name, release_id: releaseId, design_text: designText }),
  });
}

export function saveDesign(id: string, designText: string): Promise<Experiment> {
  return request<Experiment>(`/api/experiments/${encodeURIComponent(id)}/design`, {
    method: "POST",
    body: JSON.stringify({ design_text: designText }),
  });
}

export function forkDesign(id: string): Promise<Experiment> {
  return request<Experiment>(`/api/experiments/${encodeURIComponent(id)}/fork`, {
    method: "POST",
    body: JSON.stringify({}),
  });
}

export function lockExperiment(id: string, designSha256: string): Promise<Experiment> {
  return request<Experiment>(`/api/experiments/${encodeURIComponent(id)}/lock`, {
    method: "POST",
    body: JSON.stringify({ design_sha256: designSha256 }),
  });
}

export function launchExperiment(
  experimentId: string,
  mode: Launch["mode"],
  options: { maxParallelism?: number; shardCount?: number } = {},
): Promise<Launch> {
  return request<Launch>("/api/launches", {
    method: "POST",
    body: JSON.stringify({
      experiment_id: experimentId,
      mode,
      max_parallelism: options.maxParallelism,
      shard_count: options.shardCount,
    }),
  });
}
