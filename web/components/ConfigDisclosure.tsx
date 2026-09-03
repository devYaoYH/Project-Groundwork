export function ConfigDisclosure({ label = "Show declaration", value }: { label?: string; value: string }) {
  return (
    <details className="config-disclosure">
      <summary>{label}</summary>
      <pre>{value}</pre>
    </details>
  );
}
