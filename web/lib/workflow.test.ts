import assert from "node:assert/strict";
import test from "node:test";
import { backEdges, branchTargets, layoutInput, type WorkflowGraph } from "./workflow.ts";

const graph: WorkflowGraph = {
  id: "root", nodes: ["__start__", "check", "call", "end"].map((id) => ({ id, symbol: id, kind: "domain" })),
  edges: [
    { source: "__start__", target: "check", conditional: false },
    { source: "check", target: "call", conditional: true, route_key: "continue" },
    { source: "call", target: "check", conditional: false },
    { source: "check", target: "end", conditional: true, route_key: "finished" },
  ],
};
test("layout input retains cycles and branch targets without authoring a second graph", () => {
  assert.deepEqual([...backEdges(graph)], [2]);
  assert.deepEqual(branchTargets(graph, "check").map((edge) => edge.route_key), ["continue", "finished"]);
  assert.equal(layoutInput(graph).edges.length, 4);
  assert.equal(layoutInput(graph, true).layoutOptions["elk.direction"], "DOWN");
  assert.equal(layoutInput(graph).layoutOptions["elk.direction"], "RIGHT");
  assert.deepEqual(graph.nodes.map((node) => node.id), layoutInput(graph).children.map((node) => node.id));
});
test("invalid, empty and oversized topology does not reach layout", () => {
  assert.throws(() => layoutInput({ ...graph, nodes: [] }));
  assert.throws(() => layoutInput({ ...graph, nodes: [...graph.nodes, graph.nodes[0]] }));
  assert.throws(() => layoutInput({ ...graph, edges: [{ source: "missing", target: "check", conditional: false }] }));
  assert.throws(() => layoutInput({ ...graph, nodes: Array(257).fill(graph.nodes[0]) }));
});
