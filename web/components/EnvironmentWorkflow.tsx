"use client";

import dynamic from "next/dynamic";
import { useEffect, useState } from "react";
import { getEnvironmentWorkflow } from "../lib/api";
import { branchTargets, subgraphSummary, summaryLabel, type WorkflowResponse } from "../lib/workflow";

const WorkflowCanvas = dynamic(() => import("./WorkflowCanvas"), { ssr: false });

export function EnvironmentWorkflow({ id }: { id: string }) {
  const [response, setResponse] = useState<WorkflowResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [graphId, setGraphId] = useState<string | null>(null);
  const [selected, setSelected] = useState<string | null>(null);
  const [parents, setParents] = useState<string[]>([]);
  useEffect(() => {
    const controller = new AbortController();
    setResponse(null); setError(null); setSelected(null); setGraphId(null); setParents([]);
    getEnvironmentWorkflow(id, controller.signal).then((next) => {
      if (!controller.signal.aborted) { setResponse(next); setGraphId(next.workflow?.root_graph ?? null); }
    }).catch((reason: Error) => { if (!controller.signal.aborted) setError(reason.message); });
    return () => controller.abort();
  }, [id]);
  const doc = response?.workflow;
  const graph = doc?.graphs.find((entry) => entry.id === graphId);
  const node = graph?.nodes.find((entry) => entry.id === selected);
  return <section className="card section-card workflow-section" aria-labelledby="workflow-heading">
    <div className="section-heading"><h2 id="workflow-heading">Executable Workflow</h2><p>Possible execution, not a replay or communication-permission graph. Agents receive observations at the marked handoffs; domain algorithms remain opaque.</p></div>
    {error ? <p className="notice notice-error" role="alert">Workflow unavailable: {error}</p> : null}
    {!response && !error ? <p role="status">Loading executable workflow...</p> : null}
    {response?.status === "unavailable" ? <p className="notice">Executable workflow unavailable: migration pending for this environment.</p> : null}
    {response?.status === "invalid" ? <p className="notice notice-error">Invalid executable workflow. No inferred paths are shown.</p> : null}
    {response?.status === "partial" ? <p className="notice">Partial executable coverage. Only the shown boundaries are represented, not a complete episode. Surrounding driver orchestration and unknown routes are omitted, not inferred.</p> : null}
    {doc && graph ? <>
      <div className="workflow-toolbar"><span className="mono">{graph.id}</span>{parents.length > 1 ? <button className="button" onClick={() => { setGraphId(parents.at(-1) ?? null); setParents(parents.slice(0, -1)); setSelected(null); }}>Parent graph</button> : null}{graphId !== response?.workflow?.root_graph ? <button className="button" onClick={() => { setGraphId(response?.workflow?.root_graph ?? null); setSelected(null); setParents([]); }}>Root graph</button> : null}<span>Pan / zoom / fit. Dashed edges return to earlier steps. Conditional routes are alternatives, not concurrent fan-out.</span></div>
      <div className="workflow-body"><WorkflowCanvas key={graph.id} doc={doc} graph={graph} selected={selected} onSelect={setSelected} />
        <aside className="workflow-inspector" aria-label="Step inspector" aria-live="polite">
          <h3>{node?.id ?? "Inspect a step"}</h3>
          {node ? <><p>{node.kind.replaceAll("_", " ")}</p><code>{node.symbol}</code>{node.actor_selector ? <p><strong>Actor selector</strong><br /><code>{node.actor_selector}</code><br />Operation: {node.operation}</p> : null}{node.kind === "batch" ? <><p>Bounded concurrent fan-out. Join waits for every invocation; results retain call-list order.</p><p>Items: <code>{node.items_selector}</code><br />Concurrency: <code>{node.concurrency_selector}</code><br />Serialized when selector returns a key: <code>{node.serial_key_selector}</code></p></> : null}{node.source_ref ? <p>Source: <code>{node.source_ref.path}</code></p> : null}<h4>May execute next</h4><ul>{branchTargets(graph, node.id).map((edge, index) => <li key={index}><button className="text-link" onClick={() => setSelected(edge.target)}>{edge.route_key ? `${edge.route_symbol}: ${edge.route_key} → ` : ""}{edge.target}</button></li>)}</ul>{node.subgraph ? <button className="button" onClick={() => { setParents([...parents, graph.id]); setGraphId(node.subgraph ?? null); setSelected(null); }}>Open subgraph</button> : null}</> : <p>Select a node or use the keyboard-accessible outline.</p>}
        </aside>
      </div>
      <details className="workflow-outline" open><summary>Workflow outline - all steps and branch targets</summary><ol>{graph.nodes.map((entry) => {
        const summary = entry.subgraph && (entry.kind === "subgraph" || entry.kind === "batch") ? subgraphSummary(doc, entry.subgraph) : null;
        return <li key={entry.id}><button aria-pressed={entry.id === selected} onClick={() => setSelected(entry.id)}>{entry.id}</button><span>{entry.actor_selector ? `${entry.actor_selector} / ${entry.operation}` : entry.kind === "batch" ? `Bounded concurrent fan-out / ${entry.items_selector}` : entry.symbol}</span>{summary ? <span>{summaryLabel({ ...summary, fanOut: summary.fanOut || entry.kind === "batch" })}</span> : null}<span>{branchTargets(graph, entry.id).map((edge) => `${edge.route_key ? `${edge.route_symbol}: ${edge.route_key} → ` : "→ "}${edge.target}`).join("; ") || "End"}</span></li>;
      })}</ol></details>
      <p className="muted mono workflow-digest">Structure {response?.workflow?.structure_digest}. Identifies topology and bindings, not every algorithm.</p>
    </> : null}
  </section>;
}
