import { EpisodeList } from "../../components/EpisodeList";
import { Suspense } from "react";

// Becomes the experiment screen's episode table in phase 4; standing alone
// first is what proves the filtered, paginated read path end to end.
export default function EpisodesPage() {
  return <Suspense fallback={<p className="empty-state">Loading episodes...</p>}><EpisodeList /></Suspense>;
}
