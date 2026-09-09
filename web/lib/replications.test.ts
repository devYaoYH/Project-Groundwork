import assert from "node:assert/strict";
import test from "node:test";

import type { EpisodeSummary } from "./api.ts";
import { groupReplicationExecutions, replicationProgress } from "./replications.ts";

function episode(overrides: Partial<EpisodeSummary>): EpisodeSummary {
  return {
    episode_uid: "uid",
    environment_id: "demo",
    experiment_name: "experiment",
    episode_id: "experiment.cell.000",
    cell_id: "cell",
    episode_idx: 0,
    experiment_id: "experiment-id",
    release_id: null,
    item_id: null,
    attempt: 1,
    seed: 1,
    status: "COMPLETED",
    started_at: null,
    ended_at: null,
    stopped: false,
    created_at: "2026-09-09T00:00:00Z",
    metrics: {},
    ...overrides,
  };
}

test("groups physical executions under their latest logical replication", () => {
  const groups = groupReplicationExecutions([
    episode({ episode_uid: "first", attempt: 1, status: "FAILED" }),
    episode({ episode_uid: "other", episode_id: "experiment.cell.001", episode_idx: 1, attempt: 1 }),
    episode({ episode_uid: "retry", attempt: 2, status: "COMPLETED" }),
  ]);

  assert.equal(groups.length, 2);
  assert.equal(groups[0].latest.episode_uid, "retry");
  assert.equal(groups[0].latest.status, "COMPLETED");
  assert.deepEqual(groups[0].priorExecutions.map((entry) => entry.episode_uid), ["first"]);
  assert.equal(groups[1].latest.episode_uid, "other");
});

test("formats known per-cell replication progress", () => {
  assert.equal(replicationProgress(episode({ episode_idx: 0 }), { cell: 2 }), "1 / 2");
  assert.equal(replicationProgress(episode({ episode_idx: 1 }), { cell: 2 }), "2 / 2");
  assert.equal(replicationProgress(episode({ episode_idx: null }), { cell: 2 }), "-");
});
