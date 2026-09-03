import { Suspense } from "react";

import { EpisodeView } from "../../components/EpisodeView";

// An episode_uid is not knowable at build time, so this is a query-parameter
// route rather than a dynamic segment: `output: "export"` can only prerender
// segments `generateStaticParams` enumerates. Same shape as the experiment and
// design screens.
export default function EpisodePage() {
  return (
    <Suspense fallback={<p className="empty-state">Loading episode...</p>}>
      <EpisodeView />
    </Suspense>
  );
}
