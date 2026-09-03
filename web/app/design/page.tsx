import { DesignEditor } from "../../components/DesignEditor";
import { Suspense } from "react";

export default function DesignPage() {
  return <Suspense fallback={<p className="empty-state">Loading design...</p>}><DesignEditor /></Suspense>;
}
