import type { Metadata } from "next";

import { Rail } from "../components/Rail";
import "../styles/globals.css";

export const metadata: Metadata = {
  title: "Groundwork",
  description: "Research workflow control plane",
};

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="en">
      <body>
        <div className="shell">
          <Rail />
          <main className="main-content">{children}</main>
        </div>
      </body>
    </html>
  );
}
