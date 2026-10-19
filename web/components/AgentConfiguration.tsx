"use client";

import { useEffect, useState } from "react";

import { Chip } from "./Chip";
import { ConfigDisclosure } from "./ConfigDisclosure";
import { Crumb } from "./Crumb";
import { DataTable } from "./DataTable";
import { AgentBinding, AgentPool, getAgentPool } from "../lib/api";
import { useSession } from "./Session";

function credentialState(agent: AgentBinding, platform: boolean) {
  if (agent.credential === "vertexai_adc" || agent.api_format?.startsWith("vertexai")) return <Chip>Google ADC</Chip>;
  if (!agent.credential) return <span className="muted">none needed</span>;
  if (platform) return <Chip>platform managed</Chip>;
  return <span className="agent-credential"><code>{agent.credential}</code> <Chip tone={agent.credential_present ? "good" : "warn"}>{agent.credential_present ? "set" : "missing"}</Chip></span>;
}

export function AgentConfiguration() {
  const { isPlatform } = useSession();
  const [pool, setPool] = useState<AgentPool | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    getAgentPool().then(setPool).catch((reason: Error) => setError(reason.message));
  }, []);

  return (
    <>
      <Crumb items={[{ label: "agents" }]} />
      <header className="page-heading">
        <div>
          <h1>Agent configuration</h1>
          <p>Logical agent names resolve to the effective model bindings for this researcher’s workspace.</p>
        </div>
      </header>
      {error ? <p className="notice notice-error">{error}</p> : null}
      {!pool && !error ? <p className="empty-state">Loading agent configuration...</p> : null}
      {pool ? <>
        <section className="card section-card">
          <div className="section-heading">
            <div>
              <h2>Agent pool</h2>
              <p>{isPlatform ? "Experiments select these published bindings in their roster. Provider credentials are supplied by the platform and remain private." : "Experiments select these names in their roster. Credential values remain private; this only reports whether the runner can see each required variable."}</p>
            </div>
          </div>
          {pool.agents.length === 0 ? <p className="empty-state">No agent bindings were found.</p> : <DataTable
            rows={pool.agents}
            rowKey={(agent) => agent.name}
            columns={[
              { label: "name", className: "mono", render: (agent) => agent.name },
              { label: "model", className: "mono", render: (agent) => agent.model ?? "-" },
              { label: "declared inputs", render: (agent) => {
                const inputs = Array.isArray(agent.capabilities?.input) ? agent.capabilities.input : ["text"];
                return `${inputs.join(", ")}${inputs.some((input) => input !== "text") && !agent.capabilities?.evidence ? " · verification required" : ""}`;
              } },
              { label: "format", className: "mono", render: (agent) => agent.api_format ?? agent.type },
              { label: "endpoint", className: "mono", render: (agent) => agent.api_base ?? "default" },
              { label: "credential", render: (agent) => credentialState(agent, isPlatform) },
              { label: "notes", className: "muted", render: (agent) => agent.description || "-" },
            ]}
          />}
        </section>
        <section className="card section-card">
          <div className="section-heading">
            <div>
              <h2>{isPlatform ? "Versioned agent bindings" : "Manage bindings in files"}</h2>
              <p>{isPlatform ? "Shared bindings are published with each environment release. Your experiment retains the resolved configuration used by its executions." : "This screen is intentionally read-only: edit the shared pool or your local override, then reload to inspect the effective bindings."}</p>
            </div>
          </div>
          {!isPlatform ? <p className="agent-config-paths"><code>experiments/agents.yaml</code> is shared. An optional <code>agents.local.yaml</code> at the workspace root overrides entries by name for your own provider route.</p> : null}
          {pool.sources.length ? <ConfigDisclosure label="Show pool sources" value={pool.sources.join("\n")} /> : null}
        </section>
      </> : null}
    </>
  );
}
