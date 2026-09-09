import assert from "node:assert/strict";
import test from "node:test";

import { episodeQueryFromSearch, parseEpisodeQuery, serializeEpisodeQuery } from "./episode-query.ts";

test("parses plain episode-name terms and typed filters into API state", () => {
  const parsed = parseEpisodeQuery(
    "testing fork cell:cell-abc experiment:experiment-123 environment:word_guess status:COMPLETED",
    { environments: ["calendar"], statuses: ["PARTIAL"] },
  );

  assert.deepEqual(parsed.terms, ["testing", "fork"]);
  assert.deepEqual(parsed.filters, {
    q: "testing fork",
    cell_id: "cell-abc",
    experiment_id: "experiment-123",
    environment_id: ["calendar", "word_guess"],
    status: ["PARTIAL", "COMPLETED"],
  });
  assert.deepEqual(parsed.issues, []);
});

test("reports malformed typed syntax without treating it as name search", () => {
  const parsed = parseEpisodeQuery("testing cell: mystery:value status:");

  assert.deepEqual(parsed.terms, ["testing"]);
  assert.deepEqual(parsed.filters, { q: "testing" });
  assert.equal(parsed.issues.length, 3);
  assert.match(parsed.issues.map((issue) => issue.message).join(" "), /cell: needs.*Unknown filter.*status: needs/);
});

test("serializes URL state and translates legacy deep links to the common query", () => {
  const state = episodeQueryFromSearch(new URLSearchParams(
    "cell_id=cell-abc&experiment_id=experiment-123&environment_id=word_guess&status=COMPLETED&status=PARTIAL",
  ));

  assert.deepEqual(state, {
    raw: "experiment:experiment-123 cell:cell-abc",
    environments: ["word_guess"],
    statuses: ["COMPLETED", "PARTIAL"],
  });
  assert.equal(
    serializeEpisodeQuery(state),
    "q=experiment%3Aexperiment-123+cell%3Acell-abc&environment_id=word_guess&status=COMPLETED&status=PARTIAL",
  );
});
