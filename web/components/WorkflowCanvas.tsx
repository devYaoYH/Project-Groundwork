"use client";

import { useEffect, useState } from "react";
import { Background, BaseEdge, Controls, Handle, MarkerType, Position, ReactFlow, type Edge, type EdgeProps, type Node, type NodeProps } from "@xyflow/react";
import ELK, { type ElkNode } from "elkjs/lib/elk.bundled.js";
import { backEdges, edgeLabel, layoutInput, portId, routePath, subgraphSummary, summaryLabel, type WorkflowDocument, type WorkflowGraph } from "../lib/workflow";

type Port = { id: string; x: number; y: number; position: Position; type: "source" | "target" };
type WorkflowFlowNode = Node<{ label: React.ReactNode; ports: Port[] }>;
type RoutedEdge = Edge<{ path: string; labelX?: number; labelY?: number }>;

function WorkflowNode({ data }: NodeProps<WorkflowFlowNode>) {
  return <>{data.label}{data.ports.map((port) => <Handle key={port.id} id={port.id} type={port.type} position={port.position}
    isConnectable={false} style={{ left: `calc(${port.x}px - 1px)`, top: `calc(${port.y}px - var(--workflow-node-border-top, 1px))`, right: "auto", bottom: "auto", transform: "translate(-50%, -50%)" }} />)}</>;
}

function WorkflowEdge({ id, data, label, markerEnd, style }: EdgeProps<RoutedEdge>) {
  return <BaseEdge id={id} path={data!.path} markerEnd={markerEnd} style={style} label={label}
    labelX={data?.labelX} labelY={data?.labelY} labelBgPadding={[4, 2]} />;
}

const nodeTypes = { workflow: WorkflowNode };
const edgeTypes = { elk: WorkflowEdge };

export default function WorkflowCanvas({ doc, graph, selected, onSelect }: { doc: WorkflowDocument; graph: WorkflowGraph; selected: string | null; onSelect: (id: string) => void }) {
  const [nodes, setNodes] = useState<Node[]>([]);
  const [edges, setEdges] = useState<Edge[]>([]);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    let stale = false;
    setNodes([]); setEdges([]); setError(null);
    const narrow = window.matchMedia("(max-width: 760px)").matches;
    Promise.resolve().then(() => new ELK().layout<ElkNode>(layoutInput(graph, narrow))).then((layout) => {
      if (stale) return;
      const loops = backEdges(graph);
      setNodes(graph.nodes.map((node) => {
        const position = layout.children?.find((child) => child.id === node.id);
        const summary = node.subgraph && (node.kind === "subgraph" || node.kind === "batch") ? subgraphSummary(doc, node.subgraph) : null;
        return { id: node.id, type: "workflow", position: { x: position?.x ?? 0, y: position?.y ?? 0 },
          data: { label: <div><strong>{node.id}</strong><span>{node.kind === "batch" ? "bounded concurrent fan-out" : node.kind.replaceAll("_", " ")}</span>{summary ? <small>{summaryLabel({ ...summary, fanOut: summary.fanOut || node.kind === "batch" })}</small> : node.actor_selector ? <small>{node.actor_selector.split(".").at(-1)} / {node.operation}</small> : null}</div>,
            ports: position?.ports?.map((port) => ({ id: port.id, x: port.x ?? 0, y: port.y ?? 0,
              type: port.id.endsWith("source") ? "source" : "target",
              position: port.id.endsWith("source") ? Position.Right : Position.Left })) ?? [] },
          style: { width: 210, height: 86 }, className: `react-flow__node-default workflow-node workflow-node-${node.kind}` };
      }));
      setEdges(graph.edges.map((edge, index) => {
        const routed = layout.edges?.find((item) => item.id === String(index));
        const label = routed?.labels?.[0];
        return { id: String(index), source: edge.source, target: edge.target,
        sourceHandle: portId(index, "source"), targetHandle: portId(index, "target"),
        data: { path: routePath(routed?.sections ?? []), labelX: label ? (label.x ?? 0) + (label.width ?? 0) / 2 : undefined,
          labelY: label ? (label.y ?? 0) + (label.height ?? 0) / 2 : undefined },
        label: edgeLabel(edge, loops.has(index)),
        type: "elk", markerEnd: { type: MarkerType.ArrowClosed },
        className: loops.has(index) ? "workflow-back-edge" : undefined,
        ariaLabel: `${edge.source} to ${edge.target}${edge.route_key ? `: ${edge.route_key}` : ""}`,
        selectable: false };
      }));
    }).catch(() => { if (!stale) setError("Workflow layout unavailable. Use the outline to inspect every step."); });
    return () => { stale = true; };
  }, [doc, graph]);
  if (error) return <p className="notice notice-error">{error}</p>;
  if (!nodes.length) return <p role="status">Laying out executable graph...</p>;
  return <div className="workflow-canvas" aria-label="Read-only executable workflow">
    <ReactFlow nodeTypes={nodeTypes} edgeTypes={edgeTypes} nodes={nodes.map((node) => ({ ...node, selected: node.id === selected }))} edges={edges}
      onNodeClick={(_, node) => onSelect(node.id)}
      nodesDraggable={false} nodesConnectable={false} edgesReconnectable={false} deleteKeyCode={null}
      selectionKeyCode={null} multiSelectionKeyCode={null} fitView minZoom={0.15} maxZoom={2}
      proOptions={{ hideAttribution: false }}>
      <Background />
      <Controls showInteractive={false} />
    </ReactFlow>
  </div>;
}
