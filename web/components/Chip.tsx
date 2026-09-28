import { ReactNode } from "react";

export function Chip({ children, tone = "plain" }: { children: ReactNode; tone?: "plain" | "good" | "warn" | "bad" }) {
  return <span className={`chip chip-${tone}`}>{children}</span>;
}
