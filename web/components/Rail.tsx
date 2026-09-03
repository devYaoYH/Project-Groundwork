"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";

// The detail screens are query-parameter routes, so their paths are siblings of
// the list they belong to rather than children of it: `/episode/` does not
// start with `/episodes`. Each entry therefore names the prefixes it owns
// instead of deriving one from its own href.
const navigation = [
  { href: "/environments/", label: "Environments", owns: ["/environments"] },
  { href: "/episodes/", label: "Episodes", owns: ["/episodes", "/episode"] },
  { href: "/experiments/", label: "Experiments", owns: ["/experiments", "/experiment", "/design"] },
];

export function Rail() {
  // `usePathname` is null for one render during hydration of a static export.
  const pathname = usePathname() ?? "";

  return (
    <aside className="rail">
      <Link className="wordmark" href="/environments/">Groundwork</Link>
      <p className="tagline">research control plane</p>
      <nav aria-label="Primary navigation">
        <p className="rail-group">Workspace</p>
        {navigation.map((item) => {
          const active = item.owns.some(
            (prefix) => pathname === prefix || pathname.startsWith(`${prefix}/`),
          );
          return (
            <Link className={`nav-link${active ? " active" : ""}`} href={item.href} key={item.href}>
              {item.label}
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
