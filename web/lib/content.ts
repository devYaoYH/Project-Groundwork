/** Text projection for inspection; images stay structured and are rendered separately. */
export function contentText(value: unknown): string | null {
  if (typeof value === "string") return value;
  if (!Array.isArray(value)) return null;
  const text = value.flatMap((part) => part && typeof part === "object" && part.type === "text" && typeof part.text === "string" ? [part.text] : []);
  return text.length ? text.join("\n") : null;
}
