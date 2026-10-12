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

export function layoutInput(graph: WorkflowGraph, _narrow = false) {
  if (graph.nodes.length > 256 || graph.edges.length > 1024 || !graph.nodes.length) throw new Error("Invalid workflow size");
  const ids = new Set(graph.nodes.map((node) => node.id));
  if (ids.size !== graph.nodes.length || graph.edges.some((edge) => !ids.has(edge.source) || !ids.has(edge.target))) throw new Error("Invalid workflow topology");
  const loops = backEdges(graph);
  return {
    id: graph.id,
    layoutOptions: { "elk.algorithm": "layered", "elk.direction": "RIGHT", "elk.edgeRouting": "ORTHOGONAL", "elk.spacing.nodeNode": "45", "elk.layered.spacing.nodeNodeBetweenLayers": "65" },
    children: graph.nodes.map((node) => ({ id: node.id, width: 210, height: 86,
      layoutOptions: { "elk.portConstraints": "FIXED_SIDE", ...(node.id === "__start__" ? { "elk.layered.layering.layerConstraint": "FIRST_SEPARATE" } : {}) },
      ports: graph.edges.flatMap((edge, index) => [
        ...(edge.source === node.id ? [{ id: portId(index, "source"), width: 0, height: 0, layoutOptions: { "elk.port.side": "EAST" } }] : []),
        ...(edge.target === node.id ? [{ id: portId(index, "target"), width: 0, height: 0, layoutOptions: { "elk.port.side": "WEST" } }] : []),
      ]),
    })),
    edges: graph.edges.map((edge, index) => {
      const text = edgeLabel(edge, loops.has(index));
      return { id: String(index), sources: [portId(index, "source")], targets: [portId(index, "target")],
        labels: text ? [{ id: `label-${index}`, text, width: text.length * 7 + 8, height: 20 }] : [],
      };
    }),
  };
}

export function portId(index: number, end: "source" | "target") {
  return `edge-${index}-${end}`;
}

export function edgeLabel(edge: WorkflowEdge, loop: boolean) {
  return [edge.route_key, loop ? "back-edge" : null].filter(Boolean).join(" / ");
}

export type RoutePoint = { x: number; y: number };
export type RouteSection = { startPoint: RoutePoint; endPoint: RoutePoint; bendPoints?: RoutePoint[] };

export function routePath(sections: RouteSection[]) {
  if (!sections.length) throw new Error("Missing workflow route");
  return sections.map((section) => [section.startPoint, ...(section.bendPoints ?? []), section.endPoint]
    .map((point, index) => `${index ? "L" : "M"} ${point.x} ${point.y}`).join(" ")).join(" ");
}
