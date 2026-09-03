import Link from "next/link";

type CrumbItem = { label: string; href?: string };

export function Crumb({ items }: { items: CrumbItem[] }) {
  return (
    <nav className="crumb" aria-label="Breadcrumb">
      {items.map((item, index) => (
        <span key={`${item.label}-${index}`}>
          {index > 0 ? <span className="crumb-separator">/</span> : null}
          {item.href ? <Link href={item.href}>{item.label}</Link> : item.label}
        </span>
      ))}
    </nav>
  );
}
