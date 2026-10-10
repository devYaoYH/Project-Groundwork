export type WorkflowNode = {
  id: string;
  symbol: string;
  kind: "boundary" | "domain" | "agent_call" | "decision" | "subgraph";
  actor_selector?: string;
  operation?: string;
  source_ref?: { path: string; symbol: string };
  subgraph?: string;
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

export function layoutInput(graph: WorkflowGraph, narrow = false) {
  if (graph.nodes.length > 256 || graph.edges.length > 1024 || !graph.nodes.length) throw new Error("Invalid workflow size");
  const ids = new Set(graph.nodes.map((node) => node.id));
  if (ids.size !== graph.nodes.length || graph.edges.some((edge) => !ids.has(edge.source) || !ids.has(edge.target))) throw new Error("Invalid workflow topology");
  return {
    id: graph.id,
    layoutOptions: { "elk.algorithm": "layered", "elk.direction": narrow ? "DOWN" : "RIGHT", "elk.spacing.nodeNode": "45", "elk.layered.spacing.nodeNodeBetweenLayers": "65" },
    children: graph.nodes.map((node) => ({ id: node.id, width: 210, height: 86 })),
    edges: graph.edges.map((edge, index) => ({ id: String(index), sources: [edge.source], targets: [edge.target] })),
  };
}
