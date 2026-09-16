import { jsonTree, JsonTreeNode } from "../lib/json-tree";

function JsonNode({ node, label }: { node: JsonTreeNode; label?: string }) {
  const name = label ? <code className="json-tree-key">{label}</code> : null;
  if (node.kind === "scalar") {
    return <div className="json-tree-scalar">{name}<code>{node.value}</code></div>;
  }
  const description = node.kind === "array" ? `array (${node.entries.length})` : `object (${node.entries.length})`;
  const children = node.kind === "array"
    ? node.entries.map((entry, index) => <JsonNode key={index} label={String(index)} node={entry} />)
    : node.entries.map((entry) => <JsonNode key={entry.key} label={entry.key} node={entry.value} />);
  return (
    <details className="json-tree-node">
      <summary>{name}{description}</summary>
      <div className="json-tree-children">{children.length ? children : <span className="muted">empty</span>}</div>
    </details>
  );
}

export function JsonTree({ value }: { value: unknown }) {
  const node = jsonTree(value);
  if (node.kind === "scalar") return <JsonNode node={node} />;
  const children = node.kind === "array"
    ? node.entries.map((entry, index) => <JsonNode key={index} label={String(index)} node={entry} />)
    : node.entries.map((entry) => <JsonNode key={entry.key} label={entry.key} node={entry.value} />);
  return <div className="json-tree" aria-label="Item parameters">{children.length ? children : <span className="muted">empty object</span>}</div>;
}
