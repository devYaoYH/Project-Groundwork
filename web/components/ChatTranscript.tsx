import type { CSSProperties, Ref } from "react";
import { MediaPreview } from "./MediaPreview";

import { keepWhenHidingSystemEvents, laneHue, type ChatProjection, type LaneRow } from "../lib/lanes";

export function ChatTranscript({ chat, lanes, bounded, hideSystemEvents, onSeek, activeRef, environmentId }: {
  chat: ChatProjection;
  lanes: readonly LaneRow[];
  bounded: number;
  hideSystemEvents: boolean;
  onSeek: (cursor: number) => void;
  activeRef: Ref<HTMLLIElement>;
  environmentId?: string | null;
}) {
  return (
    <>
      <ol className={`chat chat-${chat.mode}`} aria-label={hideSystemEvents ? "Recorded events excluding synthetic system events" : "Recorded events"}>
        {chat.rows.map((row) => (
          <li
            className={`transcript-line chat-row chat-${row.side}${row.continued ? " chat-continued" : ""}${row.cursor === bounded ? " transcript-active" : ""}${row.cursor > bounded ? " chat-ahead" : ""}`}
            key={row.cursor}
            hidden={hideSystemEvents && !keepWhenHidingSystemEvents(row)}
            tabIndex={0}
            aria-label={`Event ${row.cursor + 1}: ${row.laneId}, ${row.type}. Press Enter or Space to seek.`}
            aria-current={row.cursor === bounded ? "step" : undefined}
            onClick={() => onSeek(row.cursor)}
            onKeyDown={(event) => {
              if (event.target !== event.currentTarget) return;
              if (event.key === "Enter" || event.key === " ") {
                event.preventDefault();
                onSeek(row.cursor);
              }
            }}
            ref={row.cursor === bounded && (!hideSystemEvents || keepWhenHidingSystemEvents(row)) ? activeRef : null}
            style={{ "--hue": laneHue(lanes, row.laneId) } as CSSProperties}
          >
            <div className={`chat-${row.kind}`}>
              {chat.mode === "group" && row.kind === "message" ? <span className="chat-avatar" aria-hidden="true">{Array.from(row.laneId)[0]?.toUpperCase()}</span> : null}
              <div className="chat-envelope">
                {row.kind === "thought" ? (
                  <details
                    className="chat-thought-text"
                    onClick={(event) => event.stopPropagation()}
                    onKeyDown={(event) => event.stopPropagation()}
                  >
                    <summary className="chat-meta" aria-label={`Expand ${row.type} by ${row.laneId} for event ${row.cursor + 1}`}>
                      <span className="chat-thought-toggle">{row.type}</span>
                      <em>{row.laneId}</em>
                      <span className="chat-thought-preview">{row.text}</span>
                    </summary>
                    <p>{row.text}</p>
                  </details>
                ) : (
                  <>
                    <div className="chat-meta">
                      {row.kind !== "divider" ? <em className={row.continued ? "visually-hidden" : undefined}>{row.laneId}</em> : null}
                      <code>{row.type}</code>
                      {row.kind === "message" && row.recipients ? <span className="chat-recipients" aria-label={`Recipients: ${row.recipients.join(", ")}`}>{"\u2192 "}{row.recipients.join(", ")}</span> : null}
                      {row.kind === "divider" && ["string", "number"].includes(typeof row.data.round) ? <span>round {String(row.data.round)}</span> : null}
                    </div>
                    {row.text ? <p>{row.text}</p> : null}
                  </>
                )}
                {environmentId ? <MediaPreview value={row.data} environmentId={environmentId} /> : null}
                <details
                  className="chat-payload"
                  onClick={(event) => event.stopPropagation()}
                  onKeyDown={(event) => event.stopPropagation()}
                >
                  <summary aria-label={`Recorded payload for event ${row.cursor + 1}`}>payload</summary>
                  <pre>{JSON.stringify(row.data, null, 2)}</pre>
                </details>
              </div>
            </div>
          </li>
        ))}
      </ol>
      {chat.rows.length === 0 ? <p className="empty-state">Nothing was said in this attempt.</p> : null}
      {hideSystemEvents && chat.rows.length > 0 && !chat.rows.some(keepWhenHidingSystemEvents) ? <p className="empty-state">Only synthetic system events were recorded in this attempt. Turn off Hide system events to inspect them.</p> : null}
    </>
  );
}
