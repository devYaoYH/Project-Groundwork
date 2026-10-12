"use client";

import { useEffect, useMemo, useRef } from "react";
import type { Lane, TraceEvent } from "../lib/api";
import { projectChat } from "../lib/chat";
import { MediaPreview } from "./MediaPreview";

export function EpisodeChat({ events, lanes, environmentId, episodeUid, cursor, onSelect }: {
  events: TraceEvent[]; lanes: Lane[] | null | undefined; environmentId: string;
  episodeUid: string; cursor: number; onSelect: (cursor: number) => void;
}) {
  const turns = useMemo(() => projectChat(events, lanes), [events, lanes]);
  const active = useRef<HTMLLIElement | null>(null);
  useEffect(() => { active.current?.scrollIntoView({ block: "nearest" }); }, [cursor]);
  return <ol className="episode-chat" aria-label="Episode conversation">
    {turns.map(turn => <li key={turn.cursor}
      className={`chat-${turn.kind}${turn.cursor <= cursor && cursor <= turn.endCursor ? " chat-active" : ""}`}
      ref={turn.cursor <= cursor && cursor <= turn.endCursor ? active : null}>
      {turn.kind === "boundary" ? <button className="text-link" onClick={() => onSelect(turn.cursor)}>{turn.label}</button> : <>
        <button className="chat-speaker text-link" onClick={() => onSelect(turn.cursor)}>{turn.laneId}</button>
        {turn.input !== null ? <div className="chat-input">
          <span className="muted">Private input</span>
          <MediaPreview value={turn.input} environmentId={environmentId} episodeUid={episodeUid} caption={`Shown to ${turn.laneId}`} />
          {turn.inputText ? <details><summary>View input text</summary><pre>{turn.inputText}</pre></details> : null}
        </div> : null}
        {turn.messages.map(message => <div className="chat-message" key={message.cursor}>
          {message.private ? <span className="muted">Private response</span> : null}
          <p onClick={() => onSelect(message.cursor)}>{message.text}</p>
        </div>)}
        {!turn.messages.length ? <p className="muted">No utterance recorded for this turn.</p> : null}
      </>}
    </li>)}
    {!turns.length ? <li className="empty-state">No conversation recorded.</li> : null}
  </ol>;
}
