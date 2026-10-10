"use client";

import { useEffect, useState } from "react";
import { Background, Controls, MarkerType, Position, ReactFlow, type Edge, type Node } from "@xyflow/react";
import ELK from "elkjs/lib/elk.bundled.js";
import { backEdges, layoutInput, type WorkflowGraph } from "../lib/workflow";

export default function WorkflowCanvas({ graph, selected, onSelect }: { graph: WorkflowGraph; selected: string | null; onSelect: (id: string) => void }) {
  const [nodes, setNodes] = useState<Node[]>([]);
  const [edges, setEdges] = useState<Edge[]>([]);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    let stale = false;
    setNodes([]); setError(null);
    const narrow = window.matchMedia("(max-width: 760px)").matches;
    Promise.resolve().then(() => new ELK().layout(layoutInput(graph, narrow))).then((layout) => {
      if (stale) return;
      const loops = backEdges(graph);
      setNodes(graph.nodes.map((node) => {
        const position = layout.children?.find((child) => child.id === node.id);
        return { id: node.id, position: { x: position?.x ?? 0, y: position?.y ?? 0 },
          data: { label: <div><strong>{node.id}</strong><span>{node.kind.replaceAll("_", " ")}</span>{node.actor_selector ? <small>{node.actor_selector.split(".").at(-1)} / {node.operation}</small> : null}</div> },
          sourcePosition: narrow ? Position.Bottom : Position.Right,
          targetPosition: narrow ? Position.Top : Position.Left,
          style: { width: 210, height: 86 }, className: `workflow-node workflow-node-${node.kind}` };
      }));
      setEdges(graph.edges.map((edge, index) => ({ id: String(index), source: edge.source, target: edge.target,
        label: [edge.route_key, loops.has(index) ? "back-edge" : null].filter(Boolean).join(" / "),
        type: "smoothstep", markerEnd: { type: MarkerType.ArrowClosed },
        className: loops.has(index) ? "workflow-back-edge" : undefined,
        ariaLabel: `${edge.source} to ${edge.target}${edge.route_key ? `: ${edge.route_key}` : ""}`,
        selectable: false })));
    }).catch(() => { if (!stale) setError("Workflow layout unavailable. Use the outline to inspect every step."); });
    return () => { stale = true; };
  }, [graph]);
  if (error) return <p className="notice notice-error">{error}</p>;
  if (!nodes.length) return <p role="status">Laying out executable graph...</p>;
  return <div className="workflow-canvas" aria-label="Read-only executable workflow">
    <ReactFlow nodes={nodes.map((node) => ({ ...node, selected: node.id === selected }))} edges={edges}
      onNodeClick={(_, node) => onSelect(node.id)}
      nodesDraggable={false} nodesConnectable={false} edgesReconnectable={false} deleteKeyCode={null}
      selectionKeyCode={null} multiSelectionKeyCode={null} fitView minZoom={0.15} maxZoom={2}
      proOptions={{ hideAttribution: false }}>
      <Background />
      <Controls showInteractive={false} />
    </ReactFlow>
  </div>;
}
