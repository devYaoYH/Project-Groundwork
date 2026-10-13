import assert from "node:assert/strict";
import test from "node:test";
import ELK, { type ElkNode } from "elkjs/lib/elk.bundled.js";
import { backEdges, branchTargets, canvasId, compoundLayoutInput, flattenLayout, graphAncestors, layoutInput, portId, reachableGraphs, resolveCanvasId, routePath, subgraphSummary, summaryLabel, type WorkflowDocument, type WorkflowGraph } from "./workflow.ts";

const document: WorkflowDocument = {
  schema_version: 1, environment_id: "test", basis: "executable_langgraph", structure_digest: "test",
  root_graph: "root", diagnostics: [], graphs: [
    { id: "root", edges: [], nodes: [
      { id: "direct", symbol: "direct", kind: "agent_call" },
      { id: "round", symbol: "round", kind: "subgraph", subgraph: "root/round" },
      { id: "reflection", symbol: "reflection", kind: "subgraph", subgraph: "root/reflection" },
    ] },
    { id: "root/round", edges: [], nodes: [
      { id: "a", symbol: "a", kind: "agent_call" },
      { id: "decision", symbol: "decision", kind: "subgraph", subgraph: "root/round/decision" },
    ] },
    { id: "root/round/decision", edges: [], nodes: [{ id: "b", symbol: "b", kind: "agent_call" }] },
    { id: "root/reflection", edges: [], nodes: [
      { id: "launch", symbol: "launch", kind: "domain" },
      { id: "batch", symbol: "batch", kind: "batch", subgraph: "root/reflection/batch" },
      { id: "join", symbol: "join", kind: "domain" },
    ] },
    { id: "root/reflection/batch", edges: [], nodes: [{ id: "call", symbol: "call", kind: "agent_call" }] },
    { id: "unreachable", edges: [], nodes: [{ id: "call", symbol: "call", kind: "agent_call" }] },
  ],
};

test("subgraph summary counts only reachable agent nodes at every depth, including batch children", () => {
  const before = structuredClone(document);
  assert.deepEqual(subgraphSummary(document, "root"), { agentCalls: 4, fanOut: true });
  assert.deepEqual(subgraphSummary(document, "root/round"), { agentCalls: 2, fanOut: false });
  assert.deepEqual(subgraphSummary(document, "root/reflection"), { agentCalls: 1, fanOut: true });
  assert.deepEqual(subgraphSummary(document, "root/reflection/batch"), { agentCalls: 1, fanOut: false });
  assert.deepEqual(document, before);
});

test("subgraph summary guards revisits, shared descendants and cyclic child links", () => {
  const doc = structuredClone(document);
  doc.graphs[0].nodes.push({ id: "shared", symbol: "shared", kind: "subgraph", subgraph: "root/round/decision" });
  doc.graphs[2].nodes.push({ id: "cycle", symbol: "cycle", kind: "subgraph", subgraph: "root" });
  assert.deepEqual(subgraphSummary(doc, "root"), { agentCalls: 4, fanOut: true });
  assert.deepEqual(subgraphSummary(doc, "root/round"), { agentCalls: 4, fanOut: true });
});

test("subgraph summary tolerates missing children and empty graphs", () => {
  const doc = structuredClone(document);
  doc.graphs.push({ id: "empty", nodes: [], edges: [] });
  doc.graphs[0].nodes.push({ id: "missing", symbol: "missing", kind: "subgraph", subgraph: "missing" });
  assert.deepEqual(subgraphSummary(doc, "empty"), { agentCalls: 0, fanOut: false });
  assert.deepEqual(subgraphSummary(doc, "missing"), { agentCalls: 0, fanOut: false });
  assert.deepEqual(subgraphSummary(doc, "root"), { agentCalls: 4, fanOut: true });
});

test("summary labels pluralize agent calls and advertise fan-out", () => {
  assert.equal(summaryLabel({ agentCalls: 0, fanOut: false }), "0 agent calls");
  assert.equal(summaryLabel({ agentCalls: 1, fanOut: false }), "1 agent call");
  assert.equal(summaryLabel({ agentCalls: 7, fanOut: false }), "7 agent calls");
  assert.equal(summaryLabel({ agentCalls: 1, fanOut: true }), "1 agent call \u00b7 fan-out");
  assert.equal(summaryLabel({ agentCalls: 7, fanOut: true }), "7 agent calls \u00b7 fan-out");
});

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
  assert.equal(layoutInput(graph).edges!.length, 4);
  assert.equal(layoutInput(graph, true).layoutOptions!["elk.direction"], "RIGHT");
  assert.equal(layoutInput(graph).layoutOptions!["elk.direction"], "RIGHT");
  assert.deepEqual(graph.nodes.map((node) => canvasId(graph.id, node.id)), layoutInput(graph).children!.map((node) => node.id));
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
    const start = layout.children!.find((node) => node.id === canvasId(graph.id, "__start__"))!;
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
        const node = layout.children!.find((node) => node.id === canvasId(graph.id, graph.edges[index][end]))!;
        const port = node.ports!.find((port) => port.id === portId(index, end, graph.id))!;
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

const compoundDoc: WorkflowDocument = { ...document, graphs: [
  { ...geometries[3], id: "root", nodes: geometries[3].nodes.map((node) => node.id === "cheap_talk_pass"
    ? { ...node, kind: "subgraph", subgraph: "child" } : node) },
  { ...geometries[2], id: "child", nodes: geometries[2].nodes.map((node) => node.id === "call"
    ? { ...node, kind: "batch", subgraph: "worker" } : node) },
  { ...geometries[5], id: "worker" },
] };

test("compound input preserves parent ports, child-owned edges, unique identities and resolution at depth", () => {
  const before = structuredClone(compoundDoc);
  const input = compoundLayoutInput(compoundDoc, "root", new Set(["child", "worker"]));
  const parent = input.children!.find((node) => node.id === "root::cheap_talk_pass")!;
  assert.equal(parent.children!.length, compoundDoc.graphs[1].nodes.length);
  assert.equal(parent.edges!.length, compoundDoc.graphs[1].edges.length);
  assert.equal(input.edges![3].sources[0], portId(3, "source", "root"));
  assert.ok(parent.ports!.some((port) => port.id === portId(3, "source", "root")));
  const flat = flattenLayout(input);
  assert.equal(new Set(flat.nodes.map((node) => node.layout.id)).size, flat.nodes.length);
  assert.deepEqual(resolveCanvasId(compoundDoc, "worker::__start__"), { graph: compoundDoc.graphs[2], node: compoundDoc.graphs[2].nodes[0] });
  assert.throws(() => resolveCanvasId(compoundDoc, "missing"));
  assert.deepEqual(graphAncestors(compoundDoc, "root", "worker"), ["root", "child"]);
  assert.deepEqual(compoundDoc, before);
});

test("compound guards reject missing, cyclic, multiply-owned, duplicate and malformed children even when collapsed", () => {
  for (const mutate of [
    (doc: WorkflowDocument) => { doc.graphs[0].nodes[2].subgraph = "missing"; },
    (doc: WorkflowDocument) => { doc.graphs[2].nodes[0].subgraph = "root"; },
    (doc: WorkflowDocument) => { doc.graphs[0].nodes[0].subgraph = "worker"; },
    (doc: WorkflowDocument) => { doc.graphs.push(doc.graphs[0]); },
    (doc: WorkflowDocument) => { doc.graphs[1].nodes.push(doc.graphs[1].nodes[0]); },
    (doc: WorkflowDocument) => { doc.graphs[2].edges[0].target = "missing"; },
    (doc: WorkflowDocument) => { doc.graphs[2].nodes[0].id = "bad::id"; },
    (doc: WorkflowDocument) => { (doc.graphs[1] as unknown as { nodes: null }).nodes = null; },
  ]) {
    const doc = structuredClone(compoundDoc); mutate(doc);
    assert.throws(() => compoundLayoutInput(doc, "root", new Set()));
    assert.throws(() => reachableGraphs(doc, "root"));
  }
});

test("expanded budgets count total drawn nodes and edges, not just the root", () => {
  const doc = structuredClone(compoundDoc);
  doc.graphs[0].nodes = Array.from({ length: 256 }, (_, index) => ({ id: String(index), symbol: "node", kind: "domain" }));
  doc.graphs[0].nodes[0].subgraph = "child";
  doc.graphs[0].edges = [];
  assert.equal(compoundLayoutInput(doc, "root", new Set()).children!.length, 256);
  assert.throws(() => compoundLayoutInput(doc, "root", new Set(["child"])));
  const edges = structuredClone(compoundDoc);
  edges.graphs[0].edges = Array(1024).fill(edges.graphs[0].edges[0]);
  assert.equal(compoundLayoutInput(edges, "root", new Set()).edges!.length, 1024);
  assert.throws(() => compoundLayoutInput(edges, "root", new Set(["child"])));
});

for (const narrow of [false, true]) test(`real compound ELK ${narrow ? "mobile" : "desktop"} keeps START leftmost, ports, orthogonal routes and label offsets at every depth`, async () => {
  const layout = await new ELK().layout(compoundLayoutInput(compoundDoc, "root", new Set(["child", "worker"]), narrow));
  function check(container: ElkNode) {
    const start = container.children!.find((node) => node.id.endsWith("::__start__"))!;
    for (const node of container.children!) {
      if (node !== start) assert.ok(start.x! + start.width! < node.x!, `START precedes ${node.id}`);
      if (node.children) check(node);
    }
  }
  check(layout);
  const flat = flattenLayout(layout);
  assert.equal(flat.nodes.length, compoundDoc.graphs.reduce((sum, graph) => sum + graph.nodes.length, 0));
  const offsets = new Map(flat.nodes.map((node) => [node.layout.id, node.absolute]));
  function checkLabels(container: ElkNode, offset = { x: 0, y: 0 }) {
    for (const edge of container.edges ?? []) for (const label of edge.labels ?? []) {
      const rendered = flat.edges.find((item) => item.id === edge.id)!.labels!.find((item) => item.id === label.id)!;
      assert.equal(rendered.x, label.x! + offset.x);
      assert.equal(rendered.y, label.y! + offset.y);
    }
    for (const child of container.children ?? []) if (child.children) checkLabels(child, offsets.get(child.id)!);
  }
  checkLabels(layout);
  for (const edge of flat.edges) {
    const [ownerId, index] = edge.id.split("::");
    const graph = compoundDoc.graphs.find((graph) => graph.id === ownerId)!;
    const original = graph.edges[Number(index)];
    for (const end of ["source", "target"] as const) {
      const node = flat.nodes.find((node) => node.layout.id === canvasId(ownerId, original[end]))!;
      const port = node.layout.ports!.find((port) => port.id === portId(Number(index), end, ownerId))!;
      assert.ok(Math.abs(port.x! - (end === "source" ? node.layout.width! : 0)) < .01);
      const endpoint = end === "source" ? edge.sections![0].startPoint : edge.sections!.at(-1)!.endPoint;
      assert.ok(Math.abs(endpoint.x - node.absolute.x - port.x!) < .01, `${edge.id} ${end} x`);
      assert.ok(Math.abs(endpoint.y - node.absolute.y - port.y!) < .01, `${edge.id} ${end} y`);
    }
    const ancestors = graphAncestors(compoundDoc, "root", ownerId);
    const obstacles = flat.nodes.filter(({ layout }) => !layout.children || !ancestors.concat(ownerId).some((id) =>
      compoundDoc.graphs.find((graph) => graph.id === layout.id.split("::")[0])!.nodes.find((node) => canvasId(layout.id.split("::")[0], node.id) === layout.id)?.subgraph === id));
    for (const section of edge.sections!) {
      const points = [section.startPoint, ...(section.bendPoints ?? []), section.endPoint];
      for (let index = 1; index < points.length; index++) {
        const a = points[index - 1], b = points[index];
        assert.ok(Math.abs(a.x - b.x) < .01 || Math.abs(a.y - b.y) < .01, `orthogonal compound route ${edge.id}: ${JSON.stringify([a, b])}`);
        for (const { layout: node, absolute: { x, y } } of obstacles) {
          const crosses = Math.abs(a.x - b.x) < .01 ? a.x > x + .01 && a.x < x + node.width! - .01 && Math.max(a.y, b.y) > y + .01 && Math.min(a.y, b.y) < y + node.height! - .01
            : a.y > y + .01 && a.y < y + node.height! - .01 && Math.max(a.x, b.x) > x + .01 && Math.min(a.x, b.x) < x + node.width! - .01;
          assert.equal(crosses, false, `${edge.id} crosses ${node.id}`);
        }
      }
    }
    for (const label of edge.labels ?? []) for (const { layout: node, absolute: { x, y } } of obstacles) {
      assert.ok(label.x! + label.width! <= x || label.x! >= x + node.width! || label.y! + label.height! <= y || label.y! >= y + node.height!, `${edge.id} label overlaps ${node.id}`);
    }
  }
});
