"use client";

import { createContext, useContext, useEffect, useRef, useState } from "react";
import { sessionMutationHeaders, setSessionCsrf } from "../lib/session";

type Principal = { owner_id: string; display_name: string; email: string };
type Config = { google_client_id: string; dev_auth: boolean; csrf_token: string };
type Phase = "checking" | "local" | "signed_out" | "signed_in" | "error";
type Session = {
  phase: Phase; user: Principal | null; config: Config | null; error: string | null;
  isPlatform: boolean; login: (path: string, data: object) => Promise<void>;
  logout: () => Promise<void>; refresh: () => Promise<void>;
};
const SessionContext = createContext<Session | null>(null);
const baseUrl = process.env.NEXT_PUBLIC_API_BASE ?? "";

export function useSession() {
  const context = useContext(SessionContext);
  if (!context) throw new Error("Session provider is missing");
  return context;
}

async function authRequest(path: string, data?: object) {
  const response = await fetch(`${baseUrl}${path}`, {
    method: data === undefined ? "GET" : "POST", credentials: "same-origin",
    headers: { "Content-Type": "application/json", ...(data === undefined ? {} : sessionMutationHeaders()) },
    body: data === undefined ? undefined : JSON.stringify(data), cache: "no-store",
  });
  const body = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(body.error || body.detail || `Sign-in request failed (${response.status})`);
  return body;
}

export function SessionProvider({ children }: { children: React.ReactNode }) {
  const [phase, setPhase] = useState<Phase>("checking");
  const [user, setUser] = useState<Principal | null>(null);
  const [config, setConfig] = useState<Config | null>(null);
  const [error, setError] = useState<string | null>(null);

  async function refresh() {
    setPhase("checking"); setError(null);
    try {
      const response = await fetch(`${baseUrl}/auth/config`, { credentials: "same-origin", cache: "no-store" });
      if (response.status === 404) {
        // The original local viewer intentionally has no identity endpoint.
        setSessionCsrf(""); setConfig(null); setUser(null); setPhase("local"); return;
      }
      if (!response.ok) throw new Error("Sign-in configuration is unavailable.");
      const nextConfig: Config = await response.json();
      if (typeof nextConfig.csrf_token !== "string" || typeof nextConfig.dev_auth !== "boolean") {
        throw new Error("Sign-in configuration is invalid.");
      }
      setConfig(nextConfig); setSessionCsrf(nextConfig.csrf_token);
      const session = await fetch(`${baseUrl}/auth/me`, { credentials: "same-origin", cache: "no-store" });
      if (session.status === 401) { setUser(null); setPhase("signed_out"); return; }
      if (!session.ok) throw new Error("Your session could not be verified.");
      const body = await session.json(); setUser(body.user); setPhase("signed_in");
    } catch (reason) {
      setUser(null); setPhase("error"); setError(reason instanceof Error ? reason.message : "Sign-in is unavailable.");
    }
  }

  useEffect(() => {
    void refresh();
    const expired = () => { setUser(null); setPhase("checking"); void refresh(); };
    window.addEventListener("groundwork:session-expired", expired);
    return () => window.removeEventListener("groundwork:session-expired", expired);
  }, []);

  async function login(path: string, data: object) {
    setError(null);
    try { const body = await authRequest(path, data); setUser(body.user); setPhase("signed_in"); }
    catch (reason) { const message = reason instanceof Error ? reason.message : "Unable to sign in."; setError(message); throw reason; }
  }
  async function logout() {
    setError(null);
    try { await authRequest("/auth/logout", {}); setUser(null); setPhase("checking"); await refresh(); }
    catch (reason) { setError(reason instanceof Error ? reason.message : "Unable to sign out."); }
  }
  return <SessionContext.Provider value={{ phase, user, config, error, isPlatform: config !== null, login, logout, refresh }}>{children}</SessionContext.Provider>;
}

type GoogleIdentity = {
  initialize: (options: { client_id: string; callback: (result: { credential: string }) => void }) => void;
  renderButton: (target: HTMLElement, options: { theme: string; size: string }) => void;
  disableAutoSelect?: () => void;
};
declare global { interface Window { google?: { accounts: { id: GoogleIdentity } }; } }

function GoogleSignIn() {
  const { config, login } = useSession();
  const target = useRef<HTMLDivElement>(null);
  const [loadError, setLoadError] = useState(false);
  useEffect(() => {
    if (!config?.google_client_id) return;
    let active = true;
    const render = () => {
      if (!active || !window.google || !target.current) return;
      window.google.accounts.id.initialize({ client_id: config.google_client_id,
        callback: (result) => { void login("/auth/google", { credential: result.credential }).catch(() => {}); } });
      target.current.replaceChildren();
      window.google.accounts.id.renderButton(target.current, { theme: "outline", size: "large" });
    };
    if (window.google) render();
    else {
      const script = document.createElement("script"); script.src = "https://accounts.google.com/gsi/client";
      script.async = true; script.onload = render; script.onerror = () => { if (active) setLoadError(true); };
      document.head.append(script);
    }
    return () => { active = false; };
  }, [config?.google_client_id]);
  return <><div ref={target} className="google-sign-in" />{loadError ? <p className="notice notice-error">Google sign-in could not load. Refresh to try again.</p> : null}</>;
}

export function SessionGate({ children }: { children: React.ReactNode }) {
  const { phase, config, error, login, refresh } = useSession();
  const [busy, setBusy] = useState(false);
  if (phase === "local" || phase === "signed_in") return <>{children}</>;
  if (phase === "checking") return <p className="empty-state" role="status">Opening your workspace…</p>;
  async function devLogin(user: string) {
    setBusy(true); try { await login("/auth/dev", { user }); } catch {} finally { setBusy(false); }
  }
  return <>
    <header className="page-heading"><div><p className="eyebrow">Your workspace</p><h1>Sign in to Groundwork.</h1><p>Choose environments, author experiments, and inspect your recorded evidence.</p></div></header>
    <section className="card session-card">
      <h2>Your experiments and results stay private.</h2>
      <p className="muted">Sign in with Google to launch experiments and download your results. Environment releases are shared across experimenters.</p>
      {error ? <p className="notice notice-error" role="alert">{error}</p> : null}
      {phase === "error" ? <button className="button" onClick={() => void refresh()}>Try again</button> : <>
        {config?.google_client_id ? <GoogleSignIn /> : null}
        {config?.dev_auth ? <div className="session-dev"><p className="muted">Local development identities</p><div className="launch-actions"><button className="button button-primary" onClick={() => void devLogin("Alice")} disabled={busy}>Continue as Alice</button><button className="button" onClick={() => void devLogin("Bob")} disabled={busy}>Continue as Bob</button></div></div> : null}
        {!config?.google_client_id && !config?.dev_auth ? <p className="notice notice-error">Google sign-in is not configured.</p> : null}
      </>}
    </section>
  </>;
}

export function SessionAccount() {
  const { user, phase, error, logout } = useSession();
  if (phase !== "signed_in" || !user) return null;
  return <section className="session-account" aria-label="Your account"><p className="rail-group">Experimenter</p><p className="session-name">{user.display_name || user.email}</p><button className="text-link" onClick={() => { window.google?.accounts.id.disableAutoSelect?.(); void logout(); }}>Sign out</button>{error ? <p className="notice notice-error">{error}</p> : null}</section>;
}
