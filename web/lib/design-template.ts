import { stringify } from "yaml";
import type { EnvironmentDetail } from "./api.ts";

export function initialDesign(detail: EnvironmentDetail): string {
  const parameters: Record<string, { randomize: true }> = {};
  for (const parameter of detail.parameters) {
    if (parameter.source === "item" && (parameter.levels?.length ?? 0) > 1) {
      parameters[parameter.name] = { randomize: true };
    }
  }
  return stringify({
    schema_version: 2,
    release: `${detail.environment_id}@${detail.release.version}`,
    parameters,
    units: { episodes_per_cell: 1 },
    roster: detail.roles.flatMap((role) => Array.from({ length: role.count }, (_, index) => {
      const kind = role.accepts.includes("scripted") ? "scripted" : role.accepts[0];
      return { id: `${role.id}_${index + 1}`, role: role.id, kind,
        binding: kind === "scripted" ? { kind: "strategy", name: "baseline" } : null };
    })),
    seed: { root: 1 },
  });
}
