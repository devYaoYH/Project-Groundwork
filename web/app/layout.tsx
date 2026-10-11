import type { Metadata } from "next";

import { Rail } from "../components/Rail";
import { SessionGate, SessionProvider } from "../components/Session";
import "@xyflow/react/dist/style.css";
import "../styles/globals.css";

export const metadata: Metadata = {
  title: "Groundwork",
  description: "Research workflow control plane",
};

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="en">
      <body>
        <SessionProvider><div className="shell">
          <Rail />
          <main className="main-content"><SessionGate>{children}</SessionGate></main>
        </div></SessionProvider>
      </body>
    </html>
  );
}
