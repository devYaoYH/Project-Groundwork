"use client";

import dynamic from "next/dynamic";
import { useCallback, useEffect, useState } from "react";
import { getEnvironmentWorkflow } from "../lib/api";
import { branchTargets, canvasId, compoundLayoutInput, graphAncestors, reachableGraphs, resolveCanvasId, subgraphSummary, summaryLabel, type WorkflowGraph, type WorkflowResponse } from "../lib/workflow";

const WorkflowCanvas = dynamic(() => import("./WorkflowCanvas"), { ssr: false });

export function EnvironmentWorkflow({ id }: { id: string }) {
  const [response, setResponse] = useState<WorkflowResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [graphId, setGraphId] = useState<string | null>(null);
  const [selected, setSelected] = useState<string | null>(null);
  const [parents, setParents] = useState<string[]>([]);
  const [expanded, setExpanded] = useState<ReadonlySet<string>>(new Set());
  const [expansionError, setExpansionError] = useState<string | null>(null);
  useEffect(() => {
    const controller = new AbortController();
    setResponse(null); setError(null); setSelected(null); setGraphId(null); setParents([]); setExpanded(new Set()); setExpansionError(null);
    getEnvironmentWorkflow(id, controller.signal).then((next) => {
      if (controller.signal.aborted) return;
      try { if (next.workflow) reachableGraphs(next.workflow, next.workflow.root_graph); }
      catch { setResponse({ status: "invalid" }); return; }
      setResponse(next); setGraphId(next.workflow?.root_graph ?? null);
    }).catch((reason: Error) => { if (!controller.signal.aborted) setError(reason.message); });
    return () => controller.abort();
  }, [id]);
  const doc = response?.workflow;
  const graph = doc?.graphs.find((entry) => entry.id === graphId);
  let topologyError = false;
  if (doc && graph) { try { reachableGraphs(doc, graph.id); } catch { topologyError = true; } }
  const selection = doc && selected && !topologyError ? resolveCanvasId(doc, selected) : null;
  const node = selection?.node;
  const changeExpansion = useCallback((next: ReadonlySet<string>) => {
    if (!doc || !graph) return;
    try {
      const input = compoundLayoutInput(doc, graph.id, next);
      const visible = new Set<string>();
      const visit = (entry: typeof input) => { visible.add(entry.id); entry.children?.forEach(visit); };
      visit(input);
      setExpanded(next); setExpansionError(null);
      setSelected((current) => current && visible.has(current) ? current : null);
    } catch { setExpansionError("Cannot expand this workflow: invalid child topology or the 256-node / 1024-edge drawing limit was exceeded."); }
  }, [doc, graph]);
  const toggle = useCallback((id: string) => {
    const next = new Set(expanded);
    if (next.has(id)) next.delete(id); else next.add(id);
    changeExpansion(next);
  }, [expanded, changeExpansion]);
  const focusGraph = (id: string | null, path: string[]) => {
    setGraphId(id); setParents(path); setSelected(null); setExpanded(new Set()); setExpansionError(null);
  };
  const openSubgraph = () => {
    if (!doc || !graph || !selection?.node.subgraph) return;
    focusGraph(selection.node.subgraph, [...parents, ...graphAncestors(doc, graph.id, selection.graph.id), selection.graph.id]);
  };
  function outline(owner: WorkflowGraph): React.ReactNode {
    return <ol>{owner.nodes.map((entry) => {
      const id = canvasId(owner.id, entry.id);
      const summary = entry.subgraph && (entry.kind === "subgraph" || entry.kind === "batch") ? subgraphSummary(doc!, entry.subgraph) : null;
      const child = entry.subgraph && expanded.has(entry.subgraph) ? doc!.graphs.find((graph) => graph.id === entry.subgraph) : null;
      return <li key={id} data-canvas-id={id}><button aria-pressed={id === selected} onClick={() => setSelected(id)}>{entry.id}</button>{entry.subgraph ? <button aria-label={`${child ? "Collapse" : "Expand"} ${entry.id}`} aria-expanded={!!child} onClick={() => toggle(entry.subgraph!)}>{child ? "Collapse" : "Expand"}</button> : null}<span>{entry.actor_selector ? `${entry.actor_selector} / ${entry.operation}` : entry.kind === "batch" ? `Bounded concurrent fan-out / ${entry.items_selector}` : entry.symbol}</span>{summary ? <span>{summaryLabel({ ...summary, fanOut: summary.fanOut || entry.kind === "batch" })}</span> : null}<span>{branchTargets(owner, entry.id).map((edge) => `${edge.route_key ? `${edge.route_symbol}: ${edge.route_key} → ` : "→ "}${edge.target}`).join("; ") || "End"}</span>{child ? outline(child) : null}</li>;
    })}</ol>;
  }
  return <section className="card section-card workflow-section" aria-labelledby="workflow-heading">
    <div className="section-heading"><h2 id="workflow-heading">Executable Workflow</h2><p>Possible execution, not a replay or communication-permission graph. Agents receive observations at the marked handoffs; domain algorithms remain opaque.</p></div>
    {error ? <p className="notice notice-error" role="alert">Workflow unavailable: {error}</p> : null}
    {!response && !error ? <p role="status">Loading executable workflow...</p> : null}
    {response?.status === "unavailable" ? <p className="notice">Executable workflow unavailable: migration pending for this environment.</p> : null}
    {response?.status === "invalid" ? <p className="notice notice-error">Invalid executable workflow. No inferred paths are shown.</p> : null}
    {response?.status === "partial" ? <p className="notice">Partial executable coverage. Only the shown boundaries are represented, not a complete episode. Surrounding driver orchestration and unknown routes are omitted, not inferred.</p> : null}
    {topologyError ? <p className="notice notice-error" role="alert">Invalid executable workflow child topology. No inferred paths are shown.</p> : null}
    {doc && graph && !topologyError ? <>
      <div className="workflow-toolbar"><span className="mono">{graph.id}</span>{parents.length ? <button className="button" onClick={() => focusGraph(parents.at(-1) ?? null, parents.slice(0, -1))}>Parent graph</button> : null}{graphId !== doc.root_graph ? <button className="button" onClick={() => focusGraph(doc.root_graph, [])}>Root graph</button> : null}<button onClick={() => changeExpansion(new Set(reachableGraphs(doc, graph.id).slice(1).map((child) => child.id)))}>Expand all</button><button onClick={() => changeExpansion(new Set())}>Collapse all</button><span>Pan / zoom / fit. Dashed edges return to earlier steps. Conditional routes are alternatives, not concurrent fan-out.</span></div>
      {expansionError ? <p className="notice notice-error" role="alert">{expansionError}</p> : null}
      <div className="workflow-body"><WorkflowCanvas key={graph.id} doc={doc} graph={graph} expanded={expanded} onToggle={toggle} selected={selected} onSelect={setSelected} />
        <aside className="workflow-inspector" aria-label="Step inspector" aria-live="polite">
          <h3>{node?.id ?? "Inspect a step"}</h3>
          {node ? <><p>{node.kind.replaceAll("_", " ")}</p><p className="mono">Graph: {selection!.graph.id}</p><code>{node.symbol}</code>{node.actor_selector ? <p><strong>Actor selector</strong><br /><code>{node.actor_selector}</code><br />Operation: {node.operation}</p> : null}{node.kind === "batch" ? <><p>Bounded concurrent fan-out. Join waits for every invocation; results retain call-list order.</p><p>Items: <code>{node.items_selector}</code><br />Concurrency: <code>{node.concurrency_selector}</code><br />Serialized when selector returns a key: <code>{node.serial_key_selector}</code></p></> : null}{node.source_ref ? <p>Source: <code>{node.source_ref.path}</code></p> : null}<h4>May execute next</h4><ul>{branchTargets(selection!.graph, node.id).map((edge, index) => <li key={index}><button className="text-link" onClick={() => setSelected(canvasId(selection!.graph.id, edge.target))}>{edge.route_key ? `${edge.route_symbol}: ${edge.route_key} → ` : ""}{edge.target}</button></li>)}</ul>{node.subgraph ? <button className="button" onClick={openSubgraph}>Open subgraph</button> : null}</> : <p>Select a node or use the keyboard-accessible outline.</p>}
        </aside>
      </div>
      <details className="workflow-outline" open><summary>Workflow outline - all steps and branch targets</summary>{outline(graph)}</details>
      <p className="muted mono workflow-digest">Structure {response?.workflow?.structure_digest}. Identifies topology and bindings, not every algorithm.</p>
    </> : null}
  </section>;
}
