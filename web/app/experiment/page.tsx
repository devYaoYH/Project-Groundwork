import { ExperimentDetailView } from "../../components/ExperimentDetailView";
import { Suspense } from "react";

export default function ExperimentPage() {
  return <Suspense fallback={<p className="empty-state">Loading experiment...</p>}><ExperimentDetailView /></Suspense>;
}
