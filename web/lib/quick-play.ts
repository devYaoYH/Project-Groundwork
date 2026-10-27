import { stringify } from "yaml";
import type { AgentBinding, EnvironmentDetail, Item, LaunchDetail, Parameter, Role } from "./api.ts";

export function environmentTitle(id: string): string {
  return id.replaceAll("_", " ").replaceAll("-", " ").replace(/\b\w/g, letter => letter.toUpperCase());
}

export function parameterOptions(parameter: Parameter): (string | number | boolean)[] {
  if (parameter.type === "boolean") return [true, false];
  if (parameter.type === "categorical") return (parameter.domain ?? []) as string[];
  const [low, high] = parameter.domain ?? [];
  if (typeof low !== "number" || typeof high !== "number") return [];
  const count = parameter.type === "integer" ? Math.min(9, high - low + 1) : 5;
  return [...new Set(Array.from({ length: count }, (_, i) => {
    const value = low + (high - low) * i / Math.max(1, count - 1);
    return parameter.type === "integer" ? Math.round(value) : Number(value.toPrecision(6));
  }))];
}

export function compatibleModels(detail: EnvironmentDetail, role: Role, agents: AgentBinding[]): AgentBinding[] {
  const required = new Set([...(detail.required_modalities ?? ["text"]), ...(role.required_modalities ?? ["text"])]);
  return agents.filter(agent => agent.type === "llm" && role.accepts.includes("llm") &&
    [...required].every(input => (agent.capabilities?.input ?? ["text"]).includes(input)) &&
    (![...required].some(input => input !== "text") || Boolean(agent.capabilities?.evidence)));
}

export function scenarioPresets(detail: EnvironmentDetail, items: Item[]): { item: Item; label: string }[] {
  const seen = new Set<string>();
  return items.flatMap(item => {
    const values = detail.parameters.filter(parameter => parameter.source === "item").map(parameter =>
      [parameter.name, item.parameter_values?.[parameter.name] ?? item.params[parameter.item_key ?? parameter.name]] as const);
    const key = JSON.stringify(values);
    if (seen.has(key)) return [];
    seen.add(key);
    return [{ item, label: values.map(([name, value]) => `${environmentTitle(name)}: ${String(value)}`).join(" · ") || item.item_id }];
  });
}

// Pins never produce extra cells. Selecting a bank row pins all its declared
// attributes together, so correlated item attributes cannot create an empty pool.
export function quickPlayDesign(detail: EnvironmentDetail, choices: Record<string, unknown>, item: Item | undefined,
  mode: "smoke" | "live", bindings: Record<string, string>, agents: AgentBinding[]): string {
  const parameters: Record<string, unknown> = {};
  for (const parameter of detail.parameters) {
    if (parameter.fixed) continue;
    if (parameter.source === "item") {
      if (item) {
        const value = item.parameter_values?.[parameter.name] ?? item.params[parameter.item_key ?? parameter.name];
        if (value === undefined) throw new Error(`Scenario is missing ${parameter.name}.`);
        parameters[parameter.name] = { pin: value };
      } else if ((parameter.levels?.length ?? 0) > 1) parameters[parameter.name] = { randomize: true };
    } else if (choices[parameter.name] !== undefined) parameters[parameter.name] = { pin: choices[parameter.name] };
  }
  const roster = detail.roles.flatMap(role => Array.from({ length: role.count }, (_, index) => {
    const id = `${role.id}_${index + 1}`;
    if (mode === "smoke") {
      if (!role.accepts.includes("scripted")) throw new Error(`${role.id} does not support scripted testing. Choose models.`);
      return { id, role: role.id, kind: "scripted", binding: { kind: "strategy", name: "baseline" },
        capabilities: { input: ["text", "image"], evidence: "groundwork:scripted-visual-v1" } };
    }
    const agent = compatibleModels(detail, role, agents).find(entry => entry.name === bindings[id]);
    if (!agent) throw new Error(`Choose a compatible model for ${environmentTitle(id)}.`);
    return { id, role: role.id, kind: "llm", binding: { kind: "agent", name: agent.name },
      ...(agent.capabilities ? { capabilities: agent.capabilities } : {}),
      ...(agent.api_format ? { api_format: agent.api_format } : {}) };
  }));
  return stringify({ schema_version: 2, release: detail.release.release_id, parameters,
    units: { episodes_per_cell: 1 }, roster, seed: { root: 1 } });
}

export function publishedQuickPlayTrace(detail: LaunchDetail): string | null {
  if (detail.provisional || detail.launch.status !== "COMPLETED" || detail.progress.completed !== 1 || detail.progress.failed) return null;
  return detail.attempts.find(attempt => attempt.status === "COMPLETED" && attempt.episode_uid)?.episode_uid ?? null;
}
