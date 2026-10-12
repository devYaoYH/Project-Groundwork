"use client";

import { useEffect, useRef, useState, type RefObject } from "react";
import { Background, BaseEdge, Controls, Handle, MarkerType, Position, ReactFlow, useReactFlow, type Edge, type EdgeProps, type Node, type NodeProps } from "@xyflow/react";
import ELK, { type ElkNode } from "elkjs/lib/elk.bundled.js";
import { backEdges, canvasId, compoundLayoutInput, edgeLabel, flattenLayout, portId, resolveCanvasId, routePath, subgraphSummary, summaryLabel, type WorkflowDocument, type WorkflowGraph } from "../lib/workflow";

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

function RestoreToggleFocus({ canvas, pending, nodes }: { canvas: RefObject<HTMLDivElement | null>; pending: RefObject<string | null>; nodes: Node[] }) {
  const { fitView } = useReactFlow();
  useEffect(() => {
    let stale = false;
    const frame = requestAnimationFrame(() => {
      void fitView().then(() => {
        if (stale || !pending.current) return;
        const node = [...(canvas.current?.querySelectorAll<HTMLElement>(".react-flow__node") ?? [])].find((node) => node.dataset.id === pending.current);
        node?.querySelector<HTMLButtonElement>(".workflow-expand")?.focus({ preventScroll: true });
        pending.current = null;
      });
    });
    return () => { stale = true; cancelAnimationFrame(frame); };
  }, [canvas, pending, nodes, fitView]);
  return null;
}

export default function WorkflowCanvas({ doc, graph, expanded, onToggle, selected, onSelect }: { doc: WorkflowDocument; graph: WorkflowGraph; expanded: ReadonlySet<string>; onToggle: (id: string) => void; selected: string | null; onSelect: (id: string) => void }) {
  const [nodes, setNodes] = useState<Node[]>([]);
  const [edges, setEdges] = useState<Edge[]>([]);
  const [error, setError] = useState<string | null>(null);
  const canvas = useRef<HTMLDivElement>(null);
  const toggleFocus = useRef<string | null>(null);
  useEffect(() => {
    let stale = false;
    setError(null);
    const narrow = window.matchMedia("(max-width: 760px)").matches;
    Promise.resolve().then(() => new ELK().layout<ElkNode>(compoundLayoutInput(doc, graph.id, expanded, narrow))).then((layout) => {
      if (stale) return;
      const flat = flattenLayout(layout);
      setNodes(flat.nodes.map(({ layout: position, parentId }) => {
        const { node } = resolveCanvasId(doc, position.id);
        const summary = node.subgraph && (node.kind === "subgraph" || node.kind === "batch") ? subgraphSummary(doc, node.subgraph) : null;
        const isExpanded = !!position.children?.length;
        return { id: position.id, parentId, domAttributes: { "data-parent-id": parentId ?? "", title: position.id }, extent: parentId ? "parent" as const : undefined, type: "workflow", position: { x: position.x ?? 0, y: position.y ?? 0 },
          data: { label: <div className="workflow-node-header"><strong>{node.id}</strong><span>{node.kind === "batch" ? "bounded concurrent fan-out" : node.kind.replaceAll("_", " ")}</span>{summary ? <small>{summaryLabel({ ...summary, fanOut: summary.fanOut || node.kind === "batch" })}</small> : node.actor_selector ? <small>{node.actor_selector.split(".").at(-1)} / {node.operation}</small> : null}{node.subgraph ? <button className="nodrag nopan workflow-expand" aria-label={`${isExpanded ? "Collapse" : "Expand"} ${node.id}`} aria-expanded={isExpanded} onKeyDown={(event) => event.stopPropagation()} onClick={(event) => { event.stopPropagation(); toggleFocus.current = position.id; onToggle(node.subgraph!); }}>{isExpanded ? "Collapse" : "Expand"}</button> : null}</div>,
            ports: position?.ports?.map((port) => ({ id: port.id, x: port.x ?? 0, y: port.y ?? 0,
              type: port.id.endsWith("source") ? "source" : "target",
              position: port.id.endsWith("source") ? Position.Right : Position.Left })) ?? [] },
          width: position.width, height: position.height, style: { width: position.width, height: position.height }, className: `react-flow__node-default workflow-node workflow-node-${node.kind}${isExpanded ? " workflow-container" : ""}` };
      }));
      setEdges(flat.edges.map((routed) => {
        const split = routed.id.lastIndexOf("::"), graphId = routed.id.slice(0, split), index = Number(routed.id.slice(split + 2));
        const owner = doc.graphs.find((entry) => entry.id === graphId)!;
        const edge = owner.edges[index], loops = backEdges(owner);
        const label = routed?.labels?.[0];
        return { id: routed.id, source: canvasId(graphId, edge.source), target: canvasId(graphId, edge.target),
        sourceHandle: portId(index, "source", graphId), targetHandle: portId(index, "target", graphId),
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
  }, [doc, graph, expanded, onToggle]);
  if (error) return <p className="notice notice-error">{error}</p>;
  if (!nodes.length) return <p role="status">Laying out executable graph...</p>;
  return <div ref={canvas} className="workflow-canvas" aria-label="Read-only executable workflow">
    <ReactFlow nodeTypes={nodeTypes} edgeTypes={edgeTypes} nodes={nodes.map((node) => ({ ...node, selected: node.id === selected }))} edges={edges}
      onNodeClick={(_, node) => onSelect(node.id)}
      nodesDraggable={false} nodesConnectable={false} edgesReconnectable={false} deleteKeyCode={null}
      selectionKeyCode={null} multiSelectionKeyCode={null} fitView minZoom={0.01} maxZoom={2}
      proOptions={{ hideAttribution: false }}>
      <RestoreToggleFocus canvas={canvas} pending={toggleFocus} nodes={nodes} />
      <Background />
      <Controls showInteractive={false} />
    </ReactFlow>
  </div>;
}
