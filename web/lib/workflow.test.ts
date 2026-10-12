import assert from "node:assert/strict";
import test from "node:test";
import ELK, { type ElkNode } from "elkjs/lib/elk.bundled.js";
import { backEdges, branchTargets, layoutInput, portId, routePath, type WorkflowGraph } from "./workflow.ts";

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
  assert.equal(layoutInput(graph, true).layoutOptions["elk.direction"], "RIGHT");
  assert.equal(layoutInput(graph).layoutOptions["elk.direction"], "RIGHT");
  assert.deepEqual(graph.nodes.map((node) => node.id), layoutInput(graph).children.map((node) => node.id));
});
test("invalid, empty and oversized topology does not reach layout", () => {
  assert.throws(() => layoutInput({ ...graph, nodes: [] }));
  assert.throws(() => layoutInput({ ...graph, nodes: [...graph.nodes, graph.nodes[0]] }));
  assert.throws(() => layoutInput({ ...graph, edges: [{ source: "missing", target: "check", conditional: false }] }));
  assert.throws(() => layoutInput({ ...graph, nodes: Array(257).fill(graph.nodes[0]) }));
});

function representative(id: string, links: string[][]): WorkflowGraph {
  return { id, nodes: [...new Set(links.flatMap(([source, target]) => [source, target]))]
    .map((id) => ({ id, symbol: id, kind: "domain" })),
    edges: links.map(([source, target, route_key]) => ({ source, target, route_key, conditional: !!route_key })) };
}

// Representative root and retry-subgraph topologies; browser coverage uses the live exports.
const geometries = [
  representative("word_guess", [["__start__", "next_turn"], ["next_turn", "guesser_call", "continue"],
    ["next_turn", "finalize", "finished"], ["guesser_call", "record_guess"], ["record_guess", "host_call"],
    ["host_call", "judge_reply"], ["judge_reply", "next_turn"], ["finalize", "__end__"]]),
  representative("buyer_seller", [["__start__", "next_round"], ["next_round", "start_round", "continue"],
    ["next_round", "finalize", "finished"], ["start_round", "seller_call"], ["seller_call", "publish_offer"],
    ["publish_offer", "buyer_call"], ["buyer_call", "record_response"], ["record_response", "trade_one_unit", "accepted"],
    ["record_response", "close_round", "rejected"], ["trade_one_unit", "close_round"], ["close_round", "next_round"], ["finalize", "__end__"]]),
  representative("negotiation", [["__start__", "guard"], ["guard", "call", "continue"], ["guard", "__end__", "bypass_decided"],
    ["call", "validate"], ["validate", "done", "valid"], ["validate", "retry_guard", "invalid"],
    ["retry_guard", "call", "retry"], ["retry_guard", "done", "exhausted"], ["done", "__end__"]]),
  representative("calendar", [["__start__", "meeting_guard"], ["meeting_guard", "cheap_talk_pass", "eligible"],
    ["meeting_guard", "finalize", "suppressed"], ["cheap_talk_pass", "participant_decisions"],
    ["participant_decisions", "resolve"], ["resolve", "round_reflections", "success"],
    ["resolve", "finalize", "depth_exceeded"], ["resolve", "meeting_guard", "no_feasible_slot"],
    ["round_reflections", "meeting_guard"], ["finalize", "__end__"]]),
  representative("self_loop_and_parallel", [["__start__", "check"], ["check", "check", "retry"],
    ["check", "__end__", "valid"], ["check", "__end__", "exhausted"]]),
  representative("root/round/reflections/worker", [["__start__", "reporter_target_call"], ["reporter_target_call", "__end__"]]),
  representative("multiple_sources", [["other_source", "join"], ["__start__", "join"], ["join", "__end__"]]),
];

for (const graph of geometries) for (const narrow of [false, true]) {
  test(`real ELK ${graph.id} ${narrow ? "mobile" : "desktop"} START is leftmost and routes avoid every node interior`, async () => {
    const layout = await new ELK().layout<ElkNode>(layoutInput(graph, narrow));
    const start = layout.children!.find((node) => node.id === "__start__")!;
    for (const node of layout.children!) {
      if (node.id !== start.id) assert.ok(start.x! + start.width! < node.x!, `START box must precede ${node.id}`);
      for (const port of node.ports ?? []) assert.ok(port.x === (port.id.endsWith("source") ? node.width : 0));
    }
    assert.equal(layout.edges?.length, graph.edges.length);
    for (const [index, edge] of layout.edges!.entries()) {
      assert.ok(edge.sections?.length);
      const sections = edge.sections!;
      assert.match(routePath(sections), /^M /);
      for (const end of ["source", "target"] as const) {
        const node = layout.children!.find((node) => node.id === graph.edges[index][end])!;
        const port = node.ports!.find((port) => port.id === portId(index, end))!;
        const point = end === "source" ? sections[0].startPoint : sections.at(-1)!.endPoint;
        assert.ok(Math.abs(point.x - node.x! - port.x!) < .01);
        assert.ok(Math.abs(point.y - node.y! - port.y!) < .01);
      }
      for (const section of sections) {
        const points = [section.startPoint, ...(section.bendPoints ?? []), section.endPoint];
        for (let i = 1; i < points.length; i++) {
          const a = points[i - 1], b = points[i];
          assert.ok(Math.abs(a.x - b.x) < .01 || Math.abs(a.y - b.y) < .01, "route must remain orthogonal");
          for (const node of layout.children!) {
            const left = node.x!, right = left + node.width!, top = node.y!, bottom = top + node.height!;
            const intersects = Math.abs(a.x - b.x) < .01
              ? a.x > left + .01 && a.x < right - .01 && Math.max(a.y, b.y) > top + .01 && Math.min(a.y, b.y) < bottom - .01
              : a.y > top + .01 && a.y < bottom - .01 && Math.max(a.x, b.x) > left + .01 && Math.min(a.x, b.x) < right - .01;
            assert.equal(intersects, false, `edge ${index} crosses ${node.id}`);
          }
        }
      }
      for (const label of edge.labels ?? []) {
        assert.ok(Number.isFinite(label.x) && Number.isFinite(label.y));
        for (const node of layout.children!) {
          assert.ok(label.x! + label.width! <= node.x! || label.x! >= node.x! + node.width! ||
            label.y! + label.height! <= node.y! || label.y! >= node.y! + node.height!, `label overlaps ${node.id}`);
        }
      }
    }
  });
}

test("route renderer preserves all ELK sections and bends instead of smoothing", () => {
  assert.equal(routePath([{ startPoint: { x: 1, y: 2 }, bendPoints: [{ x: 3, y: 2 }], endPoint: { x: 3, y: 4 } },
    { startPoint: { x: 3, y: 4 }, endPoint: { x: 5, y: 4 } }]), "M 1 2 L 3 2 L 3 4 M 3 4 L 5 4");
  assert.throws(() => routePath([]));
});
