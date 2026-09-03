import { Suspense } from "react";

import { LaunchView } from "../../components/LaunchView";

export default function LaunchPage() {
  return <Suspense fallback={<p className="empty-state">Loading launch...</p>}><LaunchView /></Suspense>;
}
