"use client";

import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { useEffect, useMemo, useState } from "react";

import { ConfigDisclosure } from "./ConfigDisclosure";
import { Crumb } from "./Crumb";
import {
  DesignValidation,
  EnvironmentDetail,
  Experiment,
  getEnvironment,
  getExperiment,
  launchExperiment,
  lockExperiment,
  saveDesign,
  validateDesign,
} from "../lib/api";
import { parseClientDesign, validateClientDesign } from "../lib/validate";

// Long enough that ordinary typing does not trigger a round trip, short enough
// that a pause feels answered. "Validate now" bypasses it entirely.
const SETTLE_MS = 600;

export function DesignEditor() {
  const search = useSearchParams();
  const id = search.get("id") || "";
  const [experiment, setExperiment] = useState<Experiment | null>(null);
  const [release, setRelease] = useState<EnvironmentDetail | null>(null);
  const [text, setText] = useState("");
  const [settledText, setSettledText] = useState("");
  const [serverResult, setServerResult] = useState<DesignValidation | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    if (!id) return;
    getExperiment(id).then(async (detail) => {
      setExperiment(detail.experiment);
      setText(detail.experiment.design_text ?? "");
      setRelease(await getEnvironment(detail.experiment.environment_id));
    }).catch((reason: Error) => setMessage(reason.message));
  }, [id]);

  // Both validators read one settled copy of the document rather than each
  // debouncing separately, so the two error lists always describe the same
  // text. A half-written line is an ordinary state of typing, not a mistake
  // worth interrupting someone over mid-keystroke.
  useEffect(() => {
    const timer = window.setTimeout(() => setSettledText(text), SETTLE_MS);
    return () => window.clearTimeout(timer);
  }, [text]);

  useEffect(() => {
    if (!experiment || !settledText) return;
    let cancelled = false;
    validateDesign(experiment.release_id, settledText).then((result) => {
      if (!cancelled) setServerResult(result);
    }).catch((reason: Error) => {
      if (!cancelled) {
        setServerResult({ valid: false, errors: [{ path: "$", message: reason.message }], plan: null });
      }
    });
    return () => { cancelled = true; };
  }, [experiment, settledText]);

  const clientErrors = useMemo(
    () => release ? validateClientDesign(parseClientDesign(settledText), release) : [],
    [release, settledText],
  );
  const pending = text !== settledText;
  const errors = [...clientErrors, ...(serverResult?.errors ?? [])];
  const plan = serverResult?.plan;
  const canLock = Boolean(experiment && serverResult?.valid && clientErrors.length === 0 && !experiment.locked_at);

  async function saveAndLock() {
    if (!experiment) return;
    setBusy(true);
    setMessage(null);
    try {
      const saved = await saveDesign(experiment.id, text);
      if (saved.id !== experiment.id) {
        window.location.assign(`/design/?id=${encodeURIComponent(saved.id)}`);
        return;
      }
      const locked = await lockExperiment(saved.id, saved.design_sha256 || "");
      setExperiment(locked);
      setMessage("Design locked. Its full episode plan is now fixed.");
    } catch (reason) {
      setMessage(reason instanceof Error ? reason.message : "Unable to lock design");
    } finally {
      setBusy(false);
    }
  }

  async function launch(mode: "live" | "smoke" | "dry_run") {
    if (!experiment) return;
    setBusy(true);
    setMessage(null);
    try {
      const result = await launchExperiment(experiment.id, mode);
      setMessage(`${mode.replace("_", " ")} launch ${result.id} is ${result.status}.`);
    } catch (reason) {
      setMessage(reason instanceof Error ? reason.message : "Unable to launch experiment");
    } finally {
      setBusy(false);
    }
  }

  if (!id) return <p className="notice notice-error">Choose an experiment from the library.</p>;
  if (!experiment || !release) return <p className="empty-state">Loading design...</p>;
  return (
    <>
      <Crumb items={[{ label: "experiments", href: "/experiments/" }, { label: experiment.name, href: `/experiment/?id=${encodeURIComponent(experiment.id)}` }, { label: "design" }]} />
      <header className="page-heading page-heading-split">
        <div><h1>{experiment.name}</h1><p>The authored document compiles to disposable episode configs. {experiment.locked_at ? "Editing will fork this preregistration." : "Lock before a live launch."}</p></div>
        <Link className="button" href={`/experiment/?id=${encodeURIComponent(experiment.id)}`}>Experiment</Link>
      </header>
      {message ? <p className={message.includes("locked") ? "notice notice-good" : "notice notice-error"}>{message}</p> : null}
      <section className="design-layout">
        <div className="design-column">
          <section className="card design-card"><div className="section-heading"><h2>Design</h2><p className="mono">{experiment.design_sha256 ?? "draft"}</p></div><textarea value={text} onChange={(event) => setText(event.target.value)} spellCheck={false} disabled={busy} /></section>
          <section className="card section-card">
            <div className="section-heading">
              <div>
                <h2>Checks</h2>
                <p>Client checks use the pinned parameter projection; the server remains authoritative. Checks run once typing pauses.</p>
              </div>
              <button onClick={() => setSettledText(text)} disabled={!pending}>{pending ? "Validate now" : "Up to date"}</button>
            </div>
            {pending ? <p className="notice notice-muted">Waiting for you to finish typing...</p>
              : errors.length ? <div className="check-list">{errors.map((error, index) => <p className="notice notice-error" key={`${error.path}-${index}`}><code>{error.path}</code> {error.message}</p>)}</div>
              : <p className="notice notice-good">Design is consistent with this release.</p>}
          </section>
        </div>
        <div className="design-column">
          <section className="card section-card"><div className="section-heading"><h2>Compiles to</h2><p>One cell per combination of factored levels.</p></div><div className="metric-grid"><div className="metric"><span>cells</span><strong>{plan?.cells.length ?? "-"}</strong></div><div className="metric"><span>episodes</span><strong>{plan?.episodes_planned ?? "-"}</strong></div></div>{plan?.preview_episode_config ? <ConfigDisclosure value={JSON.stringify(plan.preview_episode_config, null, 2)} /> : null}</section>
          <section className="card launch-card"><div className="section-heading"><h2>Launch</h2><p>Smoke and dry-run inspect a draft. Live execution requires its lock.</p></div><div className="launch-actions"><button onClick={() => launch("dry_run")} disabled={busy || !serverResult?.valid}>Dry run</button><button onClick={() => launch("smoke")} disabled={busy || !serverResult?.valid}>Smoke</button><button className="button button-primary" onClick={saveAndLock} disabled={busy || !canLock}>{experiment.locked_at ? "Locked" : "Lock preregistration"}</button><button className="button button-primary" onClick={() => launch("live")} disabled={busy || !experiment.locked_at}>Launch live</button></div></section>
        </div>
      </section>
    </>
  );
}
