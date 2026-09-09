// The one projection the episode screen needs and cannot reliably derive from
// the trace alone: which lane an event belongs to, and where the environment's
// own index advances.
//
// It is a pure function so it is testable without a browser and shared between
// the standard lane view and anything that wants the same reading of a trace.
//
// Every input here is user data or an API response, so every shape below is
// treated as optional: an episode with no events, a trace with no roster, an
// event with no payload and a payload with no speaker are all real states, and
// none of them may throw.

import type { Lane, TraceEvent } from "./api.ts";

/** Where an event with no attributable participant goes. */
export const SYSTEM_LANE = "system";

export type Mark = {
  /** The event's index in the trace: the single cursor unit for the whole screen. */
  cursor: number;
  laneId: string;
  type: string;
  /**
   * A committed action reads as a tall block, an utterance as a slim one.
   * The durable `{speaker, text}` convention is what separates them: an event
   * carrying text said something, anything else did something.
   */
  shape: "block" | "utterance";
  text: string | null;
  /** The environment's index value at this event, when it supplies one. */
  index: string | number | null;
};

export type LaneRow = {
  participant_id: string;
  kind: string;
  binding: string | null;
  role: string | null;
  /** True when the lane was discovered in the events rather than pinned in the roster. */
  declared: boolean;
  marks: Mark[];
};

export type Separator = {
  /** The cursor at which the index takes this value. */
  cursor: number;
  label: string;
  value: string | number;
};

export type LaneProjection = {
  rows: LaneRow[];
  separators: Separator[];
  cursorMax: number;
};

export type TranscriptLine = {
  cursor: number;
  laneId: string;
  type: string;
  text: string | null;
  data: Record<string, unknown>;
};

function payload(event: TraceEvent | null | undefined): Record<string, unknown> {
  const data = event?.data;
  return data && typeof data === "object" && !Array.isArray(data) ? data as Record<string, unknown> : {};
}

function asString(value: unknown): string | null {
  if (typeof value === "string") return value;
  if (typeof value === "number" || typeof value === "boolean") return String(value);
  return null;
}

/** Which participant an event belongs to, by the conventions traces use. */
export function laneOf(
  event: TraceEvent | null | undefined,
  lanes: readonly (Lane | null | undefined)[] | null | undefined = null,
): string {
  const data = payload(event);
  const named = asString(data.speaker) ?? asString(data.participant_id);
  if (named !== null) {
    const declared = Array.isArray(lanes) ? lanes : [];
    if (declared.some((lane) => lane?.participant_id === named)) return named;
    const roleMatches = declared.filter((lane) => lane?.role === named && lane.participant_id);
    if (roleMatches.length === 1) return roleMatches[0].participant_id;
    return named;
  }

  // Calendar's older records identify the acting seat numerically. A pinned
  // roster supplies the durable identity for that seat; without one, retain a
  // discoverable legacy lane instead of falsely presenting an agent action as
  // a system lifecycle event.
  const agentId = data.agent_id;
  if (typeof agentId === "number" && Number.isInteger(agentId) && agentId >= 0) {
    const participantId = lanes?.[agentId]?.participant_id;
    return typeof participantId === "string" && participantId ? participantId : `agent_${agentId}`;
  }
  return SYSTEM_LANE;
}

/** The index value an event carries, or null when it carries none. */
export function indexOf(event: TraceEvent | null | undefined, indexLabel: string | null): string | number | null {
  if (!indexLabel) return null;
  const value = payload(event)[indexLabel];
  return typeof value === "string" || typeof value === "number" ? value : null;
}

export function projectLanes(
  events: readonly (TraceEvent | null | undefined)[] | null | undefined,
  lanes: readonly (Lane | null | undefined)[] | null | undefined,
  indexLabel: string | null | undefined,
): LaneProjection {
  const list = Array.isArray(events) ? events : [];
  const label = typeof indexLabel === "string" && indexLabel ? indexLabel : null;

  const rows = new Map<string, LaneRow>();
  for (const lane of Array.isArray(lanes) ? lanes : []) {
    const id = lane?.participant_id;
    if (typeof id !== "string" || !id || rows.has(id)) continue;
    rows.set(id, {
      participant_id: id,
      kind: lane?.kind ?? "llm",
      binding: lane?.binding ?? null,
      role: typeof lane?.role === "string" && lane.role ? lane.role : null,
      declared: true,
      marks: [],
    });
  }

  const separators: Separator[] = [];
  let previousIndex: string | number | null = null;

  list.forEach((event, cursor) => {
    const laneId = laneOf(event, lanes);
    let row = rows.get(laneId);
    if (!row) {
      // A speaker the roster never mentioned is still evidence. Dropping the
      // event would hide it; inventing a lane shows it and marks it undeclared.
      row = { participant_id: laneId, kind: "unknown", binding: null, role: null, declared: false, marks: [] };
      rows.set(laneId, row);
    }
    const data = payload(event);
    const text = asString(data.text) ?? asString(data.content);
    const index = indexOf(event, label);
    row.marks.push({
      cursor,
      laneId,
      type: asString(event?.type) ?? "event",
      shape: text === null ? "block" : "utterance",
      text,
      index,
    });
    // Separators are drawn opportunistically: only where the index is present,
    // and only where it changes. An environment that supplies none gets a
    // continuous lane, which asserts nothing false.
    if (label !== null && index !== null && index !== previousIndex) {
      separators.push({ cursor, label, value: index });
      previousIndex = index;
    }
  });

  const ordered = [...rows.values()].sort(orderLanes);
  return { rows: ordered, separators, cursorMax: list.length };
}

/**
 * Pinned participants first, in roster order; discovered ones next; the
 * system lane last, because it is a catch-all rather than a participant.
 */
function orderLanes(left: LaneRow, right: LaneRow): number {
  const rank = (row: LaneRow) => (row.participant_id === SYSTEM_LANE ? 2 : row.declared ? 0 : 1);
  return rank(left) - rank(right);
}

/** The transcript beside the lanes: the durable `{speaker, text}` convention. */
export function projectTranscript(
  events: readonly (TraceEvent | null | undefined)[] | null | undefined,
  lanes: readonly (Lane | null | undefined)[] | null | undefined = null,
): TranscriptLine[] {
  const list = Array.isArray(events) ? events : [];
  return list.map((event, cursor) => ({
    cursor,
    laneId: laneOf(event, lanes),
    type: asString(event?.type) ?? "event",
    text: asString(payload(event).text) ?? asString(payload(event).content),
    data: payload(event),
  }));
}

/**
 * Colour carries participant identity only, uniformly. Saturation is not used:
 * rendering every mark saturated would assert every message was public, which
 * is false for an environment that has private messages and does not tag them.
 */
export function laneHue(rows: readonly LaneRow[], participantId: string): number {
  const position = rows.findIndex((row) => row.participant_id === participantId);
  return ((position < 0 ? rows.length : position) * 79 + 202) % 360;
}
