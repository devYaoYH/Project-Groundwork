export type Level = {
  value: unknown;
  count: number;
};

export type Parameter = {
  name: string;
  type: "continuous" | "integer" | "categorical" | "boolean";
  domain: unknown[] | null;
  fixed: boolean;
  // Where the value comes from. A design parameter is set by the researcher;
  // an item parameter is frozen in the bank, so a factor over it selects rows.
  source: "design" | "item";
  item_key: string | null;
  // Projected from the pinned bank for item parameters, null for design ones.
  levels: Level[] | null;
  // One distinct level per item: the column restates the item's identity.
  identity_grained: boolean;
};

export type Role = {
  id: string;
  count: number;
  accepts: string[];
  description: string;
};

export type Measure = {
  name: string;
  producer: "environment" | "derived";
  grain: "episode" | "sequence";
  index_label: string | null;
  unit: "joint" | "participant";
  direction: "maximize" | "minimize" | "neutral";
};

export type EnvironmentSummary = {
  environment_id: string;
  blurb: string;
  version: string;
  release_id: string;
  source_url: string;
  experiment_count: number;
};

export type EnvironmentDetail = {
  environment_id: string;
  release: {
    release_id: string;
    version: string;
    declaration_sha256: string;
    item_bank_sha256: string;
    oracle_version: string | null;
  };
  parameters: Parameter[];
  roles: Role[];
  measures: Measure[];
  item_policy: {
    mode: "enumerate" | "sample";
    bank_path: string;
    item_bank_sha256: string;
    item_count: number;
  };
  declaration_yaml: string;
};

export type Item = {
  item_id: string;
  params: Record<string, unknown>;
  oracle_result: unknown | null;
};

export type ItemPage = {
  items: Item[];
  next_cursor: string | null;
  item_bank_sha256: string;
};

export type OracleResult = {
  item_id: string;
  oracle_version: string;
  result: unknown;
};

const baseUrl = process.env.NEXT_PUBLIC_API_BASE ?? "";

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${baseUrl}${path}`, {
    ...init,
    headers: { "Content-Type": "application/json", ...init?.headers },
  });
  if (!response.ok) {
    const payload = await response.json().catch(() => ({}));
    throw new Error(payload.error ?? `Request failed (${response.status})`);
  }
  return response.json() as Promise<T>;
}

export async function listEnvironments(): Promise<EnvironmentSummary[]> {
  return (await request<{ environments: EnvironmentSummary[] }>("/api/environments")).environments;
}

export function getEnvironment(id: string): Promise<EnvironmentDetail> {
  return request<EnvironmentDetail>(`/api/environments/${encodeURIComponent(id)}`);
}

export function getItems(id: string, cursor?: string): Promise<ItemPage> {
  const query = new URLSearchParams({ limit: "100" });
  if (cursor) query.set("cursor", cursor);
  return request<ItemPage>(`/api/environments/${encodeURIComponent(id)}/items?${query}`);
}

export function runOracle(id: string, itemId: string): Promise<OracleResult> {
  return request<OracleResult>(`/api/environments/${encodeURIComponent(id)}/oracle`, {
    method: "POST",
    body: JSON.stringify({ item_id: itemId }),
  });
}
