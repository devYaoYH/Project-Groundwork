import { EnvironmentDetail } from "../../../components/EnvironmentDetail";
import { environmentIds } from "../../../lib/environments";

export function generateStaticParams() {
  return environmentIds.map((id) => ({ id }));
}

export default async function EnvironmentPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  return <EnvironmentDetail id={id} />;
}
