"use client";

import { useEffect, useState } from "react";

import { Chip } from "./Chip";
import { ConfigDisclosure } from "./ConfigDisclosure";
import { Crumb } from "./Crumb";
import { DataTable } from "./DataTable";
import { AgentBinding, AgentPool, getAgentPool } from "../lib/api";

function credentialState(agent: AgentBinding) {
  if (!agent.credential) return <span className="muted">none needed</span>;
  return <span className="agent-credential"><code>{agent.credential}</code> <Chip tone={agent.credential_present ? "good" : "warn"}>{agent.credential_present ? "set" : "missing"}</Chip></span>;
}

export function AgentConfiguration() {
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
              <p>Experiments select these names in their roster. Credential values remain private; this only reports whether the runner can see each required variable.</p>
            </div>
          </div>
          {pool.agents.length === 0 ? <p className="empty-state">No agent bindings were found.</p> : <DataTable
            rows={pool.agents}
            rowKey={(agent) => agent.name}
            columns={[
              { label: "name", className: "mono", render: (agent) => agent.name },
              { label: "model", className: "mono", render: (agent) => agent.model ?? "-" },
              { label: "format", className: "mono", render: (agent) => agent.api_format ?? agent.type },
              { label: "endpoint", className: "mono", render: (agent) => agent.api_base ?? "default" },
              { label: "credential", render: credentialState },
              { label: "notes", className: "muted", render: (agent) => agent.description || "-" },
            ]}
          />}
        </section>
        <section className="card section-card">
          <div className="section-heading">
            <div>
              <h2>Manage bindings in files</h2>
              <p>This screen is intentionally read-only: edit the shared pool or your local override, then reload to inspect the effective bindings.</p>
            </div>
          </div>
          <p className="agent-config-paths"><code>experiments/agents.yaml</code> is shared. An optional <code>agents.local.yaml</code> at the workspace root overrides entries by name for your own provider route.</p>
          {pool.sources.length ? <ConfigDisclosure label="Show pool sources" value={pool.sources.join("\n")} /> : null}
        </section>
      </> : null}
    </>
  );
}
