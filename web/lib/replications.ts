import type { EpisodeSummary } from "./api";

export type ReplicationGroup = {
  key: string;
  latest: EpisodeSummary;
  priorExecutions: EpisodeSummary[];
};

function executionOrder(left: EpisodeSummary, right: EpisodeSummary): number {
  if (left.attempt !== right.attempt) return right.attempt - left.attempt;
  const leftCreated = Date.parse(left.created_at ?? "") || 0;
  const rightCreated = Date.parse(right.created_at ?? "") || 0;
  if (leftCreated !== rightCreated) return rightCreated - leftCreated;
  return right.episode_uid.localeCompare(left.episode_uid);
}

function logicalKey(episode: EpisodeSummary): string {
  return episode.episode_id ?? episode.episode_uid;
}

export function groupReplicationExecutions(episodes: EpisodeSummary[]): ReplicationGroup[] {
  const byReplication = new Map<string, EpisodeSummary[]>();
  for (const episode of episodes) {
    const key = logicalKey(episode);
    byReplication.set(key, [...(byReplication.get(key) ?? []), episode]);
  }
  return [...byReplication.entries()]
    .map(([key, executions]) => {
      const ordered = [...executions].sort(executionOrder);
      return { key, latest: ordered[0], priorExecutions: ordered.slice(1) };
    })
    .sort((left, right) => executionOrder(left.latest, right.latest));
}

export function replicationProgress(
  episode: EpisodeSummary,
  plannedByCell: Record<string, number> = {},
): string {
  if (episode.episode_idx === null) return "-";
  const planned = episode.cell_id ? plannedByCell[episode.cell_id] : undefined;
  return planned ? `${episode.episode_idx + 1} / ${planned}` : String(episode.episode_idx + 1);
}
