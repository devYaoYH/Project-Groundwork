import type { LaunchDetail, LaunchLog } from "./api.ts";
import { notifySessionExpired } from "./session.ts";

export type LaunchStreamEvent = { kind: string; id: number | null; data: Record<string, unknown> };
export type LaunchStreamOptions = {
  signal: AbortSignal; afterId: number;
  onEvent: (event: LaunchStreamEvent) => boolean | void;
};
export const ACTIVE_LAUNCH_STATUSES = new Set(["QUEUED", "DISPATCHING", "DISPATCHED", "RUNNING", "CANCELLING"]);
export const RUNNER_LOG_LIMIT = 1000;
const RUNNER_LOG_CHARACTERS = 64000;

export class LaunchStreamError extends Error {
  readonly status: number | null;
  constructor(message: string, status: number | null = null) { super(message); this.status = status; }
}

export function boundedRunnerLogs(logs: LaunchLog[]): LaunchLog[] {
  const unique = [...new Map(logs.map(log => [log.id, log])).values()].sort((a, b) => a.id - b.id).slice(-RUNNER_LOG_LIMIT);
  const result: LaunchLog[] = [];
  let remaining = RUNNER_LOG_CHARACTERS;
  for (let index = unique.length - 1; index >= 0 && remaining > 0; index -= 1) {
    const log = unique[index];
    const line = (log.payload.line ?? "").slice(-remaining);
    result.push({ ...log, payload: { ...log.payload, line } });
    remaining -= line.length + 1;
  }
  return result.reverse();
}

export function boundedLaunchDetail(detail: LaunchDetail): LaunchDetail {
  const source = new Map((detail.runner_logs ?? []).map(log => [log.id, log]));
  const logs = boundedRunnerLogs([...source.values()]);
  const clipped = logs.length < source.size || logs.some(log => (log.payload.line ?? "").length < (source.get(log.id)?.payload.line ?? "").length);
  return { ...detail, runner_logs: logs, logs_truncated: Boolean(detail.logs_truncated || clipped) };
}

export function updateLaunchDetail(detail: LaunchDetail, event: LaunchStreamEvent): LaunchDetail {
  if (event.kind === "runner.log") {
    const log = event.data as unknown as LaunchLog;
    if (!Number.isSafeInteger(log.id) || typeof log.payload?.line !== "string") return detail;
    return boundedLaunchDetail({ ...detail, runner_logs: [...detail.runner_logs, log] });
  }
  if (event.kind === "launch.status" && typeof event.data.status === "string") {
    return { ...detail, launch: { ...detail.launch, ...event.data } as LaunchDetail["launch"] };
  }
  if (event.kind === "launch.progress" && event.data.progress && Array.isArray(event.data.attempts)) {
    return { ...detail, progress: event.data.progress as LaunchDetail["progress"], attempts: event.data.attempts as LaunchDetail["attempts"],
      provisional: typeof event.data.provisional === "boolean" ? event.data.provisional : detail.provisional,
      logs_truncated: Boolean(detail.logs_truncated || event.data.logs_truncated === true) };
  }
  const localStatuses: Record<string, string> = { "launch.started": "RUNNING", "launch.cancelling": "CANCELLING", "launch.completed": "COMPLETED",
    "launch.failed": "FAILED", "launch.cancelled": "CANCELLED", "launch.partial": "PARTIAL", "launch.unreported": "UNREPORTED", "launch.dry_run": "DRY_RUN" };
  const localStatus = localStatuses[event.kind];
  if (localStatus) return { ...detail, launch: { ...detail.launch, status: localStatus } };
  return detail;
}

function frameEvent(frame: string): LaunchStreamEvent | null {
  let name = "message";
  let id: number | null = null;
  const data: string[] = [];
  for (const line of frame.split("\n")) {
    if (line.startsWith(":")) continue;
    const colon = line.indexOf(":");
    const field = colon < 0 ? line : line.slice(0, colon);
    const value = colon < 0 ? "" : line.slice(colon + 1).replace(/^ /, "");
    if (field === "event") name = value;
    if (field === "id" && /^\d+$/.test(value)) id = Number(value);
    if (field === "data") data.push(value);
  }
  if (!data.length) return null;
  let parsed: unknown;
  try { parsed = JSON.parse(data.join("\n")); }
  catch { throw new LaunchStreamError("The live log stream returned an invalid event."); }
  if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) throw new LaunchStreamError("The live log stream returned an invalid event.");
  const record = parsed as Record<string, unknown>;
  const kind = name === "message" ? record.kind : name;
  if (typeof kind !== "string") return null;
  if (id === null && Number.isSafeInteger(record.id)) id = record.id as number;
  return { kind, id, data: record };
}

export async function consumeLaunchEvents(url: string, options: LaunchStreamOptions): Promise<void> {
  const response = await fetch(`${url}?after_id=${options.afterId}`, {
    credentials: "same-origin", cache: "no-store", signal: options.signal,
    headers: { Accept: "text/event-stream", ...(options.afterId ? { "Last-Event-ID": String(options.afterId) } : {}) },
  });
  if (!response.ok) {
    if (response.status === 401) notifySessionExpired();
    throw new LaunchStreamError(response.status === 401 ? "Your session expired. Sign in to resume live logs." : `Live logs are unavailable (${response.status}).`, response.status);
  }
  if (!response.headers.get("content-type")?.startsWith("text/event-stream") || !response.body) {
    throw new LaunchStreamError("The live log stream returned an invalid response.");
  }
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let cursor = options.afterId;
  try {
    for (;;) {
      const { value, done } = await reader.read();
      buffer += decoder.decode(value, { stream: !done });
      let boundary: RegExpExecArray | null;
      while ((boundary = /\r?\n\r?\n/.exec(buffer))) {
        const frame = buffer.slice(0, boundary.index).replace(/\r\n/g, "\n");
        buffer = buffer.slice(boundary.index + boundary[0].length);
        const event = frameEvent(frame);
        if (!event || (event.id !== null && event.id <= cursor)) continue;
        if (event.id !== null) cursor = event.id;
        if (options.onEvent(event) === false) return;
      }
      if (buffer.length > 1024 * 1024) throw new LaunchStreamError("The live log stream returned an oversized event.");
      if (done) return;
    }
  } finally { await reader.cancel().catch(() => {}); reader.releaseLock(); }
}
