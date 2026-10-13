import { Suspense } from "react";
import { QuickPlay } from "../../../../components/QuickPlay";
import { environmentIds } from "../../../../lib/environments";

export function generateStaticParams() {
  return environmentIds.map(id => ({ id }));
}

export default async function QuickPlayPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  return <Suspense fallback={<p className="empty-state">Loading Quick Play...</p>}><QuickPlay id={id} /></Suspense>;
}
