import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "Courtside",
  description: "Turn raw match video into a coach-ready scouting report.",
  icons: {
    icon:
      "data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 16 16'%3E%3Ccircle cx='8' cy='8' r='7' fill='%23c8f647'/%3E%3Cpath d='M2 4 Q8 8 2 12 M14 4 Q8 8 14 12' stroke='white' stroke-width='1.2' fill='none'/%3E%3C/svg%3E",
  },
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
