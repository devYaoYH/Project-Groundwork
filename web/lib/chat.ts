import type { Lane, TraceEvent } from "./api.ts";
import { contentText } from "./content.ts";
import { laneOf, SYSTEM_LANE } from "./lanes.ts";

export type ChatTurn = {
  kind: "turn" | "boundary";
  cursor: number;
  endCursor: number;
  laneId: string;
  label: string | null;
  input: unknown;
  inputText: string | null;
  messages: { cursor: number; text: string; private: boolean }[];
};

function dataOf(event: TraceEvent | null | undefined): Record<string, unknown> {
  return event?.data && typeof event.data === "object" && !Array.isArray(event.data) ? event.data : {};
}

/** Group recorded seat inputs with their following utterances; never invent input. */
export function projectChat(events: readonly (TraceEvent | null | undefined)[] | null | undefined,
  lanes: readonly (Lane | null | undefined)[] | null | undefined = null): ChatTurn[] {
  const turns: ChatTurn[] = [];
  let current: ChatTurn | null = null;
  for (const [cursor, event] of (events ?? []).entries()) {
    const data = dataOf(event);
    const type = event?.type ?? "event";
    const laneId = laneOf(event, lanes);
    if (/^(round|trial|game|episode)_(start|end)$/.test(type)) {
      const index = data.round ?? data.trial;
      const label = typeof data.text === "string" ? data.text :
        `${type.replaceAll("_", " ")}${index !== undefined ? ` · ${String(index)}` : ""}`;
      turns.push({ kind: "boundary", cursor, endCursor: cursor, laneId: SYSTEM_LANE,
        label, input: null, inputText: null, messages: [] });
      current = null;
      continue;
    }
    if (type === "observation" || type === "seat_observation") {
      const observation = data.observation && typeof data.observation === "object" ? data.observation as Record<string, unknown> : data;
      const input = observation.content ?? null;
      current = { kind: "turn", cursor, endCursor: cursor, laneId, label: null, input,
        inputText: contentText(input), messages: [] };
      turns.push(current);
      continue;
    }
    const text = typeof data.text === "string" ? data.text : contentText(data.content);
    if (text && laneId !== SYSTEM_LANE) {
      if (!current || current.laneId !== laneId) {
        current = { kind: "turn", cursor, endCursor: cursor, laneId, label: null,
          input: null, inputText: null, messages: [] };
        turns.push(current);
      }
      current.messages.push({ cursor, text, private: data.visibility === "private" });
      current.endCursor = cursor;
    } else if (current && laneId === current.laneId) {
      current.endCursor = cursor;
    } else if (laneId !== SYSTEM_LANE && current && laneId !== current.laneId) {
      current = null;
    }
  }
  return turns;
}
