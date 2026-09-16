export type JsonTreeNode =
  | { kind: "scalar"; value: string }
  | { kind: "object"; entries: { key: string; value: JsonTreeNode }[] }
  | { kind: "array"; entries: JsonTreeNode[] };

function scalar(value: unknown): string {
  if (typeof value === "string") return JSON.stringify(value);
  if (typeof value === "number") return Number.isFinite(value) ? String(value) : JSON.stringify(value);
  if (typeof value === "boolean" || value === null) return String(value);
  if (typeof value === "undefined") return "undefined";
  if (typeof value === "bigint") return `${value}n`;
  return String(value);
}

// API item parameters are JSON, but this presentation helper remains total so
// a malformed or manually supplied value can still be inspected safely.
export function jsonTree(value: unknown): JsonTreeNode {
  if (Array.isArray(value)) return { kind: "array", entries: value.map(jsonTree) };
  if (value && typeof value === "object") {
    return {
      kind: "object",
      entries: Object.entries(value as Record<string, unknown>).map(([key, entry]) => ({ key, value: jsonTree(entry) })),
    };
  }
  return { kind: "scalar", value: scalar(value) };
}

export function jsonPreview(value: unknown): string {
  const node = jsonTree(value);
  if (node.kind === "scalar") return node.value;
  const count = node.kind === "array" ? node.entries.length : node.entries.length;
  return node.kind === "array" ? `array (${count})` : `object (${count})`;
}
