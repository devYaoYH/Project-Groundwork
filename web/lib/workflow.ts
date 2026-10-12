export type WorkflowNode = {
  id: string;
  symbol: string;
  kind: "boundary" | "domain" | "agent_call" | "decision" | "subgraph" | "batch";
  actor_selector?: string;
  operation?: string;
  source_ref?: { path: string; symbol: string };
  subgraph?: string;
  items_selector?: string;
  concurrency_selector?: string;
  serial_key_selector?: string;
};
export type WorkflowEdge = { source: string; target: string; conditional: boolean; route_key?: string; route_symbol?: string };
export type WorkflowGraph = { id: string; nodes: WorkflowNode[]; edges: WorkflowEdge[] };
export type WorkflowDocument = {
  schema_version: number;
  environment_id: string;
  basis: "executable_langgraph";
  structure_digest: string;
  root_graph: string;
  graphs: WorkflowGraph[];
  diagnostics: { code: string; graph?: string; node?: string }[];
};
export type WorkflowResponse = { status: "available" | "partial" | "unavailable" | "invalid"; reason?: string; workflow?: WorkflowDocument };

export type SubgraphSummary = { agentCalls: number; fanOut: boolean };

export function subgraphSummary(doc: WorkflowDocument, graphId: string): SubgraphSummary {
  const summary = { agentCalls: 0, fanOut: false }, visited = new Set<string>();
  function visit(id: string) {
    if (visited.has(id)) return;
    visited.add(id);
    for (const node of doc.graphs.find((graph) => graph.id === id)?.nodes ?? []) {
      if (node.kind === "agent_call") summary.agentCalls++;
      if (node.kind === "batch") summary.fanOut = true;
      if (node.subgraph) visit(node.subgraph);
    }
  }
  visit(graphId);
  return summary;
}

export function summaryLabel(summary: SubgraphSummary): string {
  return `${summary.agentCalls} agent call${summary.agentCalls === 1 ? "" : "s"}${summary.fanOut ? " \u00b7 fan-out" : ""}`;
}

export function branchTargets(graph: WorkflowGraph, id: string) {
  return graph.edges.filter((edge) => edge.source === id);
}

export function backEdges(graph: WorkflowGraph): Set<number> {
  const active = new Set<string>(), visited = new Set<string>(), result = new Set<number>();
  function visit(id: string) {
    if (visited.has(id)) return;
    visited.add(id); active.add(id);
    graph.edges.forEach((edge, index) => {
      if (edge.source !== id) return;
      if (active.has(edge.target)) result.add(index);
      else visit(edge.target);
    });
    active.delete(id);
  }
  visit("__start__");
  graph.nodes.forEach((node) => visit(node.id));
  return result;
}

import type { ElkNode, ElkExtendedEdge } from "elkjs/lib/elk.bundled.js";

export const canvasId = (graphId: string, nodeId: string) => `${graphId}::${nodeId}`;

export function resolveCanvasId(doc: WorkflowDocument, id: string): { graph: WorkflowGraph; node: WorkflowNode } {
  for (const graph of doc.graphs) {
    const node = graph.nodes.find((node) => canvasId(graph.id, node.id) === id);
    if (node) return { graph, node };
  }
  throw new Error("Unknown workflow node");
}

export function reachableGraphs(doc: WorkflowDocument, graphId: string): WorkflowGraph[] {
  if (!Array.isArray(doc.graphs) || doc.graphs.some((graph) => !graph || typeof graph.id !== "string" ||
    !Array.isArray(graph.nodes) || !Array.isArray(graph.edges) ||
    graph.nodes.some((node) => !node || typeof node.id !== "string" || typeof node.symbol !== "string" ||
      !["boundary", "domain", "agent_call", "decision", "subgraph", "batch"].includes(node.kind) ||
      (node.subgraph !== undefined && typeof node.subgraph !== "string")) ||
    graph.edges.some((edge) => !edge || typeof edge.source !== "string" || typeof edge.target !== "string"))) {
    throw new Error("Malformed workflow document");
  }
  const graphs = new Map(doc.graphs.map((graph) => [graph.id, graph]));
  if (graphs.size !== doc.graphs.length) throw new Error("Duplicate workflow graph");
  const result: WorkflowGraph[] = [], visited = new Set<string>();
  function visit(id: string) {
    const graph = graphs.get(id);
    if (!graph || visited.has(id) || id.includes("::")) throw new Error("Invalid workflow child topology");
    visited.add(id);
    if (visited.size > 256 || !graph.nodes.length || graph.nodes.length > 256 || graph.edges.length > 1024) throw new Error("Invalid workflow size");
    const ids = new Set(graph.nodes.map((node) => node.id));
    if (ids.size !== graph.nodes.length || graph.nodes.some((node) => !node.id || node.id.includes("::")) ||
      graph.edges.some((edge) => !ids.has(edge.source) || !ids.has(edge.target))) throw new Error("Invalid workflow topology");
    result.push(graph);
    for (const node of graph.nodes) if (node.subgraph) visit(node.subgraph);
  }
  visit(graphId);
  return result;
}

export function graphAncestors(doc: WorkflowDocument, root: string, target: string): string[] {
  reachableGraphs(doc, root);
  function find(id: string, path: string[]): string[] | null {
    if (id === target) return path;
    for (const node of doc.graphs.find((graph) => graph.id === id)!.nodes) {
      if (node.subgraph) {
        const found = find(node.subgraph, [...path, id]);
        if (found) return found;
      }
    }
    return null;
  }
  const path = find(root, []);
  if (!path) throw new Error("Unreachable workflow graph");
  return path;
}

const layoutOptions = { "elk.algorithm": "layered", "elk.direction": "RIGHT", "elk.edgeRouting": "ORTHOGONAL",
  "elk.hierarchyHandling": "INCLUDE_CHILDREN", "elk.spacing.nodeNode": "45", "elk.layered.spacing.nodeNodeBetweenLayers": "65" };

export function compoundLayoutInput(doc: WorkflowDocument, graphId: string, expanded: ReadonlySet<string>, _narrow = false): ElkNode {
  const graphs = new Map(reachableGraphs(doc, graphId).map((graph) => [graph.id, graph]));
  let nodes = 0, edges = 0;
  function build(id: string, containerId: string): ElkNode {
    const graph = graphs.get(id)!;
    nodes += graph.nodes.length; edges += graph.edges.length;
    if (nodes > 256 || edges > 1024) throw new Error("Invalid expanded workflow size");
    const loops = backEdges(graph);
    return { id: containerId, layoutOptions: { ...layoutOptions },
      children: graph.nodes.map((node) => {
        const id = canvasId(graph.id, node.id);
        const child = node.subgraph && expanded.has(node.subgraph) ? build(node.subgraph, id) : null;
        return { id, width: 210, height: node.subgraph ? 120 : 86, ...(child ? { children: child.children, edges: child.edges } : {}),
          layoutOptions: { ...layoutOptions, "elk.portConstraints": "FIXED_SIDE",
            ...(child ? { "elk.padding": "[top=120,left=24,bottom=24,right=24]" } : {}),
            ...(node.id === "__start__" ? { "elk.layered.layering.layerConstraint": "FIRST_SEPARATE" } : {}) },
          ports: graph.edges.flatMap((edge, index) => [
            ...(edge.source === node.id ? [{ id: portId(index, "source", graph.id), width: 0, height: 0, layoutOptions: { "elk.port.side": "EAST" } }] : []),
            ...(edge.target === node.id ? [{ id: portId(index, "target", graph.id), width: 0, height: 0, layoutOptions: { "elk.port.side": "WEST" } }] : []),
          ]),
        };
      }),
      edges: graph.edges.map((edge, index) => {
        const text = edgeLabel(edge, loops.has(index)), id = canvasId(graph.id, String(index));
        return { id, sources: [portId(index, "source", graph.id)], targets: [portId(index, "target", graph.id)],
          labels: text ? [{ id: `${id}-label`, text, width: text.length * 7 + 8, height: 20 }] : [] };
      }),
    };
  }
  return build(graphId, graphId);
}

export function layoutInput(graph: WorkflowGraph, narrow = false): ElkNode {
  return compoundLayoutInput({ graphs: [graph] } as WorkflowDocument, graph.id, new Set(), narrow);
}

export function portId(index: number, end: "source" | "target", graphId: string) {
  return `edge-${canvasId(graphId, String(index))}-${end}`;
}

export function edgeLabel(edge: WorkflowEdge, loop: boolean) {
  return [edge.route_key, loop ? "back-edge" : null].filter(Boolean).join(" / ");
}

export type RoutePoint = { x: number; y: number };
export type RouteSection = { startPoint: RoutePoint; endPoint: RoutePoint; bendPoints?: RoutePoint[] };

export function flattenLayout(layout: ElkNode) {
  const nodes: { layout: ElkNode; parentId?: string; absolute: RoutePoint }[] = [];
  const edges: ElkExtendedEdge[] = [];
  const offsets = new Map<string, RoutePoint>([[layout.id, { x: 0, y: 0 }]]);
  const pending: { edge: ElkExtendedEdge; owner: string }[] = [];
  function visit(container: ElkNode, offset: RoutePoint) {
    for (const child of container.children ?? []) {
      const absolute = { x: offset.x + (child.x ?? 0), y: offset.y + (child.y ?? 0) };
      offsets.set(child.id, absolute);
      nodes.push({ layout: child, parentId: container === layout ? undefined : container.id, absolute });
      visit(child, absolute);
    }
    for (const edge of container.edges ?? []) pending.push({ edge, owner: container.id });
  }
  visit(layout, { x: 0, y: 0 });
  for (const { edge, owner } of pending) {
    // ELK edge geometry is relative to its containing graph, not the source node.
    const offset = offsets.get(edge.container ?? owner);
    if (!offset) throw new Error("Unknown workflow edge container");
    const point = (point: RoutePoint) => ({ x: point.x + offset.x, y: point.y + offset.y });
    edges.push({ ...edge, sections: edge.sections?.map((section) => ({ ...section,
      startPoint: point(section.startPoint), endPoint: point(section.endPoint), bendPoints: section.bendPoints?.map(point) })),
      labels: edge.labels?.map((label) => ({ ...label, x: (label.x ?? 0) + offset.x, y: (label.y ?? 0) + offset.y })) });
  }
  return { nodes, edges };
}

export function routePath(sections: RouteSection[]) {
  if (!sections.length) throw new Error("Missing workflow route");
  return sections.map((section) => [section.startPoint, ...(section.bendPoints ?? []), section.endPoint]
    .map((point, index) => `${index ? "L" : "M"} ${point.x} ${point.y}`).join(" ")).join(" ");
}
