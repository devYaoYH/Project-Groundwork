import { parse } from "yaml";

import type { EnvironmentDetail, Parameter } from "./api.ts";

export type ClientIssue = { path: string; message: string };

type Scalar = string | number | boolean;

export type ClientDisposition = {
  factor?: Scalar[];
  pin?: Scalar;
  randomize?: boolean;
};

export type ClientDesign = {
  release?: string;
  parameters?: Record<string, ClientDisposition>;
  units?: { episodes_per_cell?: number };
  roster?: { id?: string; role?: string | null; kind?: string; binding?: string | null }[];
  seed?: { mode?: string; root?: number };
};

export function parseClientDesign(text: string): ClientDesign | null {
  try {
    const value = parse(text);
    return value && typeof value === "object" && !Array.isArray(value) ? value as ClientDesign : null;
  } catch {
    return null;
  }
}

// This mirrors the fast, local subset of Python validation. The server still
// validates the full pinned bank before a lock or launch, including joint item
// strata that are intentionally not sent to the browser.
export function validateClientDesign(design: ClientDesign | null, detail: EnvironmentDetail): ClientIssue[] {
  if (!design) return [{ path: "$", message: "Invalid YAML." }];
  const issues: ClientIssue[] = [];
  const parameters = design.parameters ?? {};
  // The declaration is an API response too: a release that answers with a
  // missing collection must produce validation issues, never a crash in the
  // editor the researcher is mid-keystroke in.
  const declared = detail.parameters ?? [];
  const byName = new Map(declared.map((parameter) => [parameter.name, parameter]));
  const expectedRelease = `${detail.environment_id}@${detail.release.version}`;
  if (design.release !== detail.release.release_id && design.release !== expectedRelease) {
    issues.push({ path: "release", message: `Design pins ${String(design.release)}, not ${expectedRelease}.` });
  }

  for (const [name, disposition] of Object.entries(parameters)) {
    const parameter = byName.get(name);
    const path = `parameters.${name}`;
    if (!parameter) {
      issues.push({ path, message: `Unknown parameter ${name}.` });
      continue;
    }
    // A key typed without a body yet parses to null, and a scalar or list is a
    // shape the disposition fields cannot be read off at all. Both are ordinary
    // states of a half-written document, so they report rather than throw.
    if (disposition === null || typeof disposition !== "object" || Array.isArray(disposition)) {
      issues.push({ path, message: "Expected {factor: [...]}, {pin: ...}, or {randomize: true}." });
      continue;
    }
    const chosen = Number(disposition.factor !== undefined) + Number(disposition.pin !== undefined) + Number(disposition.randomize !== undefined);
    if (chosen !== 1) issues.push({ path, message: "Set exactly one of factor, pin, or randomize." });
    if (disposition.randomize === false) issues.push({ path, message: "randomize must be true when selected." });
    if (parameter.fixed) issues.push({ path, message: "This parameter is fixed by the release." });
    if (disposition.randomize !== undefined && parameter.source !== "item") {
      issues.push({ path, message: "This design-sourced parameter must be factored or pinned." });
    }
    if (disposition.factor !== undefined && disposition.factor !== null && !Array.isArray(disposition.factor)) {
      issues.push({ path: `${path}.factor`, message: "factor takes a list of levels." });
      continue;
    }
    const values = disposition.factor ?? (disposition.pin !== undefined ? [disposition.pin] : []);
    for (const value of values) {
      if (!matches(parameter, value)) {
        issues.push({ path, message: `Level ${String(value)} is outside the declared domain.` });
      } else if (parameter.source === "item" && !(parameter.levels ?? []).some((level) => level.value === value)) {
        issues.push({ path, message: `Level ${String(value)} names no stratum in the pinned item bank.` });
      }
    }
  }

  for (const parameter of declared) {
    if (parameter.source === "item" && (parameter.levels?.length ?? 0) > 1 && !parameters[parameter.name]) {
      const levels = parameter.levels?.map((level) => String(level.value)).join(", ") ?? "";
      issues.push({ path: `parameters.${parameter.name}`, message: `Missing disposition; bank levels: ${levels}.` });
    }
  }

  // A roster mid-edit holds half-written entries: a bare "- " parses to null,
  // and the whole key may not be a list yet.
  const rawRoster = design.roster;
  if (rawRoster !== undefined && rawRoster !== null && !Array.isArray(rawRoster)) {
    issues.push({ path: "roster", message: "roster takes a list of participants." });
    return issues;
  }
  const roster = (rawRoster ?? []).filter(
    (participant): participant is NonNullable<typeof participant> =>
      participant !== null && typeof participant === "object" && !Array.isArray(participant),
  );
  if (roster.length !== (rawRoster ?? []).length) {
    issues.push({ path: "roster", message: "Each participant needs an id, kind, and binding." });
  }
  const ids = roster.map((participant) => participant.id).filter(Boolean);
  if (ids.length !== new Set(ids).size) issues.push({ path: "roster", message: "Participant ids must be unique." });
  const rolesById = new Map((detail.roles ?? []).map((role) => [role.id, role]));
  const expectedRoleCount = (detail.roles ?? []).reduce((total, role) => total + (role.count ?? 0), 0);
  // Arity is about what the author wrote, so it counts the entries on the page
  // rather than the ones complete enough to inspect below.
  if ((rawRoster ?? []).length !== expectedRoleCount) {
    issues.push({ path: "roster", message: `Release requires ${expectedRoleCount} participants.` });
  }
  roster.forEach((participant, index) => {
    if (participant.kind !== "human" && !participant.binding) {
      issues.push({ path: `roster[${index}].binding`, message: "A non-human participant needs a binding." });
    }
    const role = typeof participant.role === "string" ? rolesById.get(participant.role) : undefined;
    if (!participant.role) {
      issues.push({ path: `roster[${index}].role`, message: "Select one declared environment role." });
    } else if (!role) {
      issues.push({ path: `roster[${index}].role`, message: "This role is not declared by the release." });
    } else if (participant.kind && !role.accepts.includes(participant.kind)) {
      issues.push({ path: `roster[${index}].kind`, message: `This role accepts ${role.accepts.join(", ")}.` });
    }
  });
  for (const role of detail.roles ?? []) {
    const assigned = roster.filter((participant) => participant.role === role.id).length;
    if (assigned !== role.count) {
      issues.push({ path: "roster", message: `Role ${role.id} requires exactly ${role.count} participant(s).` });
    }
  }

  if (detail.item_policy?.mode === "sample" && design.seed?.mode === "static") {
    issues.push({ path: "seed.mode", message: "Static seeds are incompatible with sampled items." });
  }
  return issues;
}

function matches(parameter: Parameter, value: unknown) {
  if (parameter.type === "boolean" && typeof value !== "boolean") return false;
  if (parameter.type === "integer" && (!Number.isInteger(value) || typeof value !== "number")) return false;
  if (parameter.type === "continuous" && (typeof value !== "number" || Number.isNaN(value))) return false;
  if (parameter.type === "categorical" && typeof value !== "string") return false;
  // Item domains are a browser projection of the bank rather than a declared
  // range. Let the strata check name that source of truth directly.
  if (parameter.source === "item") return true;
  if (!parameter.domain) return true;
  if (parameter.type === "continuous" || parameter.type === "integer") {
    const [minimum, maximum] = parameter.domain as number[];
    return typeof value === "number" && value >= minimum && value <= maximum;
  }
  return (parameter.domain as unknown[]).includes(value);
}
