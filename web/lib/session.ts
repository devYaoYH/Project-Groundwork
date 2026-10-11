// The CSRF challenge is public session state, not a credential. Session cookies
// are HttpOnly and remain entirely under the browser's cookie handling.
let csrfToken = "";

export function setSessionCsrf(value: string) { csrfToken = value; }
export function sessionMutationHeaders(): Record<string, string> {
  return csrfToken ? { "X-CSRF-Token": csrfToken } : {};
}

export function notifySessionExpired() {
  if (typeof window !== "undefined") window.dispatchEvent(new Event("groundwork:session-expired"));
}
