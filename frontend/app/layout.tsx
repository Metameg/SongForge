import type { Metadata } from "next";
import type { ReactNode } from "react";

export const metadata: Metadata = {
  title: "SongForge",
  description: "A synchronized, prompt-fed global radio.",
};

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="en">
      <body
        style={{
          margin: 0,
          fontFamily: "system-ui, sans-serif",
          background: "#0b0b0f",
          color: "#f2f2f2",
        }}
      >
        {children}
      </body>
    </html>
  );
}
