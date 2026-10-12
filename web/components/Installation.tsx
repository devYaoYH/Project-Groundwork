"use client";

import { useEffect, useRef, useState, type KeyboardEvent } from "react";
import { installationCommands, installationPrompts, repositoryUrl } from "../lib/installation";

function CopyText({ text, label }: { text: string; label: string }) {
  const [state, setState] = useState<"ready" | "copied" | "manual">("ready");
  const fallback = useRef<HTMLTextAreaElement>(null);

  useEffect(() => {
    if (state === "manual") { fallback.current?.focus(); fallback.current?.select(); }
  }, [state]);

  async function copy() {
    try {
      if (!navigator.clipboard?.writeText) throw new Error("Clipboard unavailable");
      await navigator.clipboard.writeText(text);
      setState("copied");
    } catch { setState("manual"); }
  }

  return <div className="installation-copy">
    <button type="button" className="installation-copy-button" aria-label={label} onClick={() => void copy()}>
      <svg width="15" height="15" viewBox="0 0 20 20" fill="none" aria-hidden="true">
        <rect x="7" y="7" width="10" height="11" rx="1.5" stroke="currentColor" strokeWidth="1.5" />
        <path d="M13 4V3.5A1.5 1.5 0 0 0 11.5 2h-8A1.5 1.5 0 0 0 2 3.5v8A1.5 1.5 0 0 0 3.5 13H4" stroke="currentColor" strokeWidth="1.5" />
      </svg>
      {state === "copied" ? "Copied" : "Copy"}
    </button>
    <span className="installation-copy-status" role="status">
      {state === "copied" ? "Copied to clipboard." : state === "manual" ? "Select this text and copy it." : ""}
    </span>
    {state === "manual" ? <textarea ref={fallback} className="installation-copy-fallback"
      aria-label={`${label} manually`} readOnly value={text} rows={5} onFocus={event => event.currentTarget.select()} /> : null}
  </div>;
}

export function Installation() {
  const [method, setMethod] = useState<"agent" | "manual">("agent");
  const tabs = useRef<(HTMLButtonElement | null)[]>([]);
  function moveTab(event: KeyboardEvent<HTMLButtonElement>, index: number) {
    if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
    event.preventDefault();
    const next = event.key === "Home" ? 0 : event.key === "End" ? 1 : 1 - index;
    setMethod(next === 0 ? "agent" : "manual"); tabs.current[next]?.focus();
  }

  return <section className="card onboarding-card installation-card" aria-labelledby="installation-heading">
    <div className="section-heading">
      <div><p className="eyebrow">Installation</p><h2 id="installation-heading">Get started with Groundwork</h2></div>
      <p>Run the stack locally or add a game to the framework.</p>
    </div>
    <div className="installation-tabs" role="tablist" aria-label="Installation method">
      {(["agent", "manual"] as const).map((value, index) => <button key={value} type="button"
        ref={element => { tabs.current[index] = element; }} role="tab" id={`installation-${value}-tab`}
        aria-selected={method === value} aria-controls={`installation-${value}-panel`} tabIndex={method === value ? 0 : -1}
        onClick={() => setMethod(value)} onKeyDown={event => moveTab(event, index)}>
        {value === "agent" ? "Install with a coding agent" : "Manual install"}
      </button>)}
    </div>
    <div role="tabpanel" id="installation-agent-panel" aria-labelledby="installation-agent-tab" hidden={method !== "agent"}>
      <p className="installation-intro">Copy a prompt into Codex, Claude Code, Cursor, or your preferred coding agent.</p>
      <div className="installation-prompts">
        {installationPrompts.map(item => <article className="installation-prompt" key={item.id}>
          <div className="installation-prompt-heading"><h3>{item.title}</h3><p>{item.description}</p></div>
          <p className="installation-prompt-text"><strong>Prompt: </strong>{item.prompt}</p>
          <CopyText text={item.prompt} label={`Copy ${item.title.toLowerCase()} prompt`} />
        </article>)}
      </div>
    </div>
    <div role="tabpanel" id="installation-manual-panel" aria-labelledby="installation-manual-tab" hidden={method !== "manual"}>
      <p className="installation-intro">Install Git and Docker with Compose, then run these commands. The first build may take a few minutes.</p>
      <div className="installation-command-block"><pre><code>{installationCommands}</code></pre>
        <CopyText text={installationCommands} label="Copy installation commands" /></div>
      <p className="installation-note">Open <a className="text-link" href="http://localhost:8080">localhost:8080</a>,
        then enter the workspace and continue as Alice or Bob. Scripted smoke runs need no API keys or cloud account.
        See the installation guide for other ports, verification, and the Python development setup.</p>
    </div>
    <div className="installation-resources" aria-label="Developer documentation">
      <a className="text-link" href={`${repositoryUrl}/blob/main/docs/INSTALLATION.md`}>Installation guide</a>
      <a className="text-link" href={`${repositoryUrl}/blob/main/skills/groundwork-development/SKILL.md`}>Development skill</a>
      <a className="text-link" href={`${repositoryUrl}/blob/main/AGENTS.md`}>Contributor instructions</a>
    </div>
  </section>;
}
