"use client";

import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { useEffect, useRef, useState } from "react";
import { Crumb } from "./Crumb";
import { AgentPool, EnvironmentDetail, ItemPage, LaunchDetail, createDesignExperiment, getAgentPool,
  getEnvironment, getItems, getLaunch, launchExperiment, lockExperiment, validateDesign } from "../lib/api";
import { ACTIVE_LAUNCH_STATUSES } from "../lib/launch-stream";
import { compatibleModels, environmentTitle, parameterOptions, publishedQuickPlayTrace, quickPlayDesign, scenarioPresets } from "../lib/quick-play";

export function QuickPlay({ id }: { id: string }) {
  const search = useSearchParams();
  const launchId = search.get("launch") ?? "";
  const [detail, setDetail] = useState<EnvironmentDetail | null>(null);
  const [pool, setPool] = useState<AgentPool>({ agents: [], sources: [] });
  const [items, setItems] = useState<ItemPage | null>(null);
  const [itemId, setItemId] = useState("");
  const [choices, setChoices] = useState<Record<string, unknown>>({});
  const [bindings, setBindings] = useState<Record<string, string>>({});
  const [mode, setMode] = useState<"smoke" | "live">("smoke");
  const [error, setError] = useState<string | null>(null);
  const [poolError, setPoolError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [loadingItems, setLoadingItems] = useState(false);
  const [stage, setStage] = useState("");
  const [createdId, setCreatedId] = useState("");
  const [launch, setLaunch] = useState<LaunchDetail | null>(null);
  const submitting = useRef(false);

  useEffect(() => {
    let cancelled = false;
    Promise.all([getEnvironment(id), getItems(id)]).then(([environment, page]) => {
      if (cancelled) return;
      setDetail(environment); setItems(page);
      if (environment.roles.some(role => !role.accepts.includes("scripted"))) setMode("live");
    }).catch(reason => { if (!cancelled) setError(reason.message); });
    getAgentPool().then(agents => { if (!cancelled) setPool(agents); })
      .catch(reason => { if (!cancelled) setPoolError(reason.message); });
    return () => { cancelled = true; };
  }, [id]);

  useEffect(() => {
    if (!detail) return;
    setBindings(current => Object.fromEntries(detail.roles.flatMap(role => Array.from({ length: role.count }, (_, index) => {
      const seat = `${role.id}_${index + 1}`;
      return [seat, current[seat] ?? compatibleModels(detail, role, pool.agents)[0]?.name ?? ""];
    }))));
  }, [detail, pool]);

  useEffect(() => {
    if (!launchId) return;
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout>;
    async function refresh() {
      try {
        const next = await getLaunch(launchId);
        if (cancelled) return;
        setLaunch(next); setError(null);
        const trace = publishedQuickPlayTrace(next);
        if (trace) { window.location.assign(`/episode/?uid=${encodeURIComponent(trace)}`); return; }
        if (ACTIVE_LAUNCH_STATUSES.has(next.launch.status) || next.provisional) timer = setTimeout(refresh, 2000);
      } catch (reason) {
        if (cancelled) return;
        setError(reason instanceof Error ? reason.message : "Unable to check episode progress.");
        timer = setTimeout(refresh, 5000);
      }
    }
    void refresh();
    return () => { cancelled = true; clearTimeout(timer); };
  }, [launchId]);

  async function moreItems() {
    if (!items?.next_cursor || loadingItems) return;
    setLoadingItems(true);
    try {
      const next = await getItems(id, items.next_cursor);
      setItems(current => ({ ...next, items: [...(current?.items ?? []), ...next.items] }));
    } catch (reason) { setError(reason instanceof Error ? reason.message : "Unable to load scenarios."); }
    finally { setLoadingItems(false); }
  }

  async function play() {
    if (!detail || submitting.current || createdId) return;
    submitting.current = true; setBusy(true); setError(null);
    let created = false;
    try {
      const text = quickPlayDesign(detail, choices, items?.items.find(item => item.item_id === itemId), mode, bindings, pool.agents);
      setStage("Checking episode settings...");
      const validation = await validateDesign(detail.release.release_id, text);
      if (!validation.valid) throw new Error(validation.errors.map(issue => issue.message).join(" "));
      if (validation.plan?.cells.length !== 1 || validation.plan.episodes_planned !== 1) throw new Error("Quick Play requires exactly one episode.");
      setStage("Preparing your episode...");
      const experiment = await createDesignExperiment(`Quick Play · ${environmentTitle(id)} · ${new Date().toISOString()}`, detail.release.release_id, text);
      created = true; setCreatedId(experiment.id);
      await lockExperiment(experiment.id, experiment.design_sha256 ?? "");
      setStage("Starting your episode...");
      const result = await launchExperiment(experiment.id, mode, { maxParallelism: 1 });
      window.location.assign(`/environments/${id}/play/?launch=${encodeURIComponent(result.id)}`);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Unable to start episode.");
      setStage(created ? "Your setup was saved. Check its run record before starting another episode." : "");
    } finally { submitting.current = false; setBusy(false); }
  }

  const title = environmentTitle(id);
  const scenarios = detail ? scenarioPresets(detail, items?.items ?? []) : [];
  const selectedScenario = scenarios.find(scenario => scenario.item.item_id === itemId)?.label;
  const terminal = launch && !ACTIVE_LAUNCH_STATUSES.has(launch.launch.status) && !launch.provisional;
  const modelMissing = mode === "live" && detail?.roles.some(role => Array.from({ length: role.count }, (_, index) => `${role.id}_${index + 1}`)
    .some(seat => !compatibleModels(detail, role, pool.agents).some(agent => agent.name === bindings[seat])));
  return <>
    <Crumb items={[{ label: "environments", href: "/environments/" }, { label: title, href: `/environments/${id}/` }, { label: "quick play" }]} />
    <header className="page-heading page-heading-split"><div><p className="eyebrow">Quick Play · one episode</p><h1>{title}</h1><p>Choose a scenario and participants. Run an episode, then explore its trace.</p></div><Link className="button" href={`/environments/${id}/`}>Details</Link></header>
    {error ? <p role="alert" className="notice notice-error">{error}</p> : null}
    {launchId ? <section className="card quick-play-progress">
      <h2>{terminal ? launch.launch.status === "COMPLETED" ? "Episode ended" : "Episode needs attention" : "Playing your episode"}</h2>
      <p role="status">{launch ? `${launch.launch.status.toLowerCase().replaceAll("_", " ")} · ${launch.progress.completed} of 1 episode saved` : "Checking episode progress..."}</p>
      {!terminal ? <p className="muted">Your trace will open once the episode finishes saving. You can leave this page and return to this run.</p> : <p className="muted">{launch.launch.error ?? "Open the run record to inspect its outcome and any saved evidence."}</p>}
      <div className="launch-actions"><Link className="button" href={`/launch/?id=${encodeURIComponent(launchId)}`}>View run and logs</Link>{terminal ? <Link className="button button-primary" href={`/environments/${id}/play/`}>Play another episode</Link> : null}</div>
    </section> : !detail ? <p className="empty-state">{error ? "Episode setup is unavailable. Reload to try again." : "Loading episode setup..."}</p> :
      <form className="quick-play-layout" onSubmit={event => { event.preventDefault(); void play(); }}>
        <fieldset className="quick-play-fields" disabled={busy || Boolean(createdId)}>
          <section className="card quick-play-section"><h2>Scenario</h2><p>Choose a scenario preset. The environment selects an item with these settings.</p>
            <label className="quick-play-control"><span>Scenario</span><select aria-label="Scenario" value={itemId} onChange={event => setItemId(event.target.value)}><option value="">Auto-select</option>{scenarios.map(({ item, label }) => <option key={item.item_id} value={item.item_id}>{label}</option>)}</select></label>
            {items?.next_cursor ? <button type="button" onClick={moreItems} disabled={loadingItems}>{loadingItems ? "Loading..." : "Load more scenarios"}</button> : null}
            <div className="quick-play-control-grid">{detail.parameters.filter(parameter => parameter.source === "design" && !parameter.fixed && parameterOptions(parameter).length).map(parameter => <label className="quick-play-control" key={parameter.name}><span>{environmentTitle(parameter.name)}</span><select aria-label={environmentTitle(parameter.name)} value={choices[parameter.name] === undefined ? "" : JSON.stringify(choices[parameter.name])} onChange={event => setChoices(current => ({ ...current, [parameter.name]: event.target.value === "" ? undefined : JSON.parse(event.target.value) }))}><option value="">Environment default</option>{parameterOptions(parameter).map(value => <option key={JSON.stringify(value)} value={JSON.stringify(value)}>{String(value)}</option>)}</select></label>)}</div>
          </section>
          <section className="card quick-play-section"><h2>Participants</h2><p>Try scripted participants or choose a model for each seat.</p>
            <label className="quick-play-control"><span>Run with</span><select aria-label="Run with" value={mode} onChange={event => setMode(event.target.value as "smoke" | "live")}><option value="smoke" disabled={detail.roles.some(role => !role.accepts.includes("scripted"))}>Scripted participants · no API key needed</option><option value="live" disabled={detail.roles.some(role => !role.accepts.includes("llm"))}>Models</option></select></label>
            {mode === "live" ? <><p className="notice notice-muted">Model runs use your configured providers and may incur usage charges.</p>{poolError ? <p role="alert" className="notice notice-error">{poolError}</p> : null}<div className="quick-play-control-grid">{detail.roles.flatMap(role => Array.from({ length: role.count }, (_, index) => {
              const seat = `${role.id}_${index + 1}`;
              const models = compatibleModels(detail, role, pool.agents);
              return <label className="quick-play-control" key={seat}><span>{environmentTitle(role.id)}{role.count > 1 ? ` ${index + 1}` : ""}</span><select aria-label={`${environmentTitle(role.id)}${role.count > 1 ? ` ${index + 1}` : ""}`} value={bindings[seat] ?? ""} onChange={event => setBindings(current => ({ ...current, [seat]: event.target.value }))}>{!models.length ? <option value="">No compatible models configured</option> : models.map(agent => <option key={agent.name} value={agent.name}>{agent.model ?? agent.name} · {agent.name}</option>)}</select>{role.description ? <small>{role.description}</small> : null}</label>;
            }))}</div>{modelMissing ? <p className="muted">This environment needs a compatible model for every seat. <Link className="text-link" href="/agents/">View agent configuration</Link></p> : null}</> : <p className="muted">{detail.roles.reduce((sum, role) => sum + role.count, 0)} scripted participants will play the environment’s baseline strategies.</p>}
          </section>
        </fieldset>
        <aside className="card quick-play-summary"><p className="eyebrow">Ready to try</p><h2>One episode</h2><dl><dt>Environment</dt><dd>{title}</dd><dt>Scenario</dt><dd>{selectedScenario || "Auto-selected"}</dd><dt>Participants</dt><dd>{mode === "smoke" ? "Scripted" : "Models"}</dd><dt>Release</dt><dd>{detail.release.version}</dd></dl><button className="button button-primary" type="submit" disabled={busy || Boolean(createdId) || Boolean(modelMissing)}>{busy ? "Preparing..." : "Run episode"}</button>{stage ? <p role="status">{stage}</p> : null}{createdId ? <Link className="text-link" href={`/experiment/?id=${encodeURIComponent(createdId)}`}>View saved run record</Link> : <p>Your settings are saved automatically. The trace opens when the episode completes.</p>}</aside>
      </form>}
  </>;
}
