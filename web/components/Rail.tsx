"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";

const navigation = [
  { href: "/environments/", label: "Environments" },
  { href: "/experiments/", label: "Experiments", pending: true },
];

export function Rail() {
  const pathname = usePathname();

  return (
    <aside className="rail">
      <Link className="wordmark" href="/environments/">Groundwork</Link>
      <p className="tagline">research control plane</p>
      <nav aria-label="Primary navigation">
        <p className="rail-group">Workspace</p>
        {navigation.map((item) => {
          const active = pathname.startsWith(item.href.replace(/\/$/, ""));
          return (
            <Link className={`nav-link${active ? " active" : ""}`} href={item.href} key={item.href}>
              {item.label}
              {item.pending ? <span>soon</span> : null}
            </Link>
          );
        })}
      </nav>
      <div className="rail-note">
        <p>Shared, versioned, read-only.</p>
        <p>Each declaration defines what a design may vary.</p>
      </div>
    </aside>
  );
}
