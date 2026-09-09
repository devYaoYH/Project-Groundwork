import Link from "next/link";

import { Chip } from "./Chip";
import { EpisodeSummary } from "../lib/api";

const TONE: Record<string, "plain" | "good" | "warn"> = {
  COMPLETED: "good",
  PARTIAL: "warn",
  STOPPED: "warn",
  FAILED: "warn",
  CANCELLED: "warn",
  UNREPORTED: "warn",
};

export function ReplicationHistory({ executions }: { executions: EpisodeSummary[] }) {
  if (executions.length === 0) return <span className="muted">-</span>;
  return (
    <details className="replication-history">
      <summary>{executions.length} earlier physical {executions.length === 1 ? "run" : "runs"}</summary>
      <ul>
        {executions.map((episode) => (
          <li key={episode.episode_uid}>
            <Link href={`/episode/?uid=${encodeURIComponent(episode.episode_uid)}`}>
              Open physical run {episode.attempt}
            </Link>
            <Chip tone={TONE[episode.status] ?? "plain"}>{episode.status}</Chip>
          </li>
        ))}
      </ul>
    </details>
  );
}
