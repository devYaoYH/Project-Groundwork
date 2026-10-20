"use client";

import { useEffect, useRef, useState } from "react";
import { useRouter } from "next/navigation";

import { getExperiment, launchExperiment, type RetryOption } from "../lib/api";

export function RetryControls({ experimentId, episodeIds, options, label = "Retry incomplete" }: {
  experimentId: string;
  episodeIds?: string[];
  options?: RetryOption[];
  label?: string;
}) {
  const router = useRouter();
  const [loaded, setLoaded] = useState<RetryOption[]>([]);
  const [busy, setBusy] = useState(false);
  const pending = useRef(false);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    if (options !== undefined) return;
    let cancelled = false;
    setLoaded([]);
    getExperiment(experimentId).then(detail => {
      if (!cancelled) setLoaded(detail.retry_options ?? []);
    }).catch(reason => {
      if (!cancelled) setError(reason instanceof Error ? reason.message : "Unable to check retry availability");
    });
    return () => { cancelled = true; };
  }, [experimentId, options]);
  const choices = (options ?? loaded).map(option => ({
    ...option,
    episode_ids: episodeIds ? option.episode_ids.filter(id => episodeIds.includes(id)) : option.episode_ids,
  })).filter(option => option.episode_ids.length > 0);
  if (!choices.length && !error) return null;
  return <div>
    <div className="launch-actions">{choices.map(option => <button key={option.mode} disabled={busy}
      title={`Create new attempts for ${option.episode_ids.length} incomplete episode(s) using the pinned experiment runtime. Earlier attempts remain available.`}
      onClick={async () => {
        if (pending.current) return;
        pending.current = true; setBusy(true); setError(null);
        try {
          const launch = await launchExperiment(experimentId, option.mode, {
            maxParallelism: option.max_parallelism, retryIncomplete: true, episodeIds: option.episode_ids,
          });
          router.push(`/launch/?id=${encodeURIComponent(launch.id)}`);
        } catch (reason) {
          setError(reason instanceof Error ? reason.message : "Unable to retry episodes");
          pending.current = false; setBusy(false);
        }
      }}>{busy ? "Starting retry…" : `${label} · ${option.mode.replace("_", " ")}`}</button>)}</div>
    {error ? <p className="notice notice-error" role="alert">{error}</p> : null}
  </div>;
}
