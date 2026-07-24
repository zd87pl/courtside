import Link from "next/link";
import type { User } from "@/lib/db";
import { LogoutButton } from "./forms";

export function Shell({ user, title, crumb, children }: {
  user: User | null;
  title: string;
  crumb?: string;
  children: React.ReactNode;
}) {
  return (
    <div className="shell">
      <aside className="side">
        <div className="brand"><span className="ball" /> Courtside</div>
        <nav className="nav">
          <Link href="/dashboard">Dashboard</Link>
          {user && <Link href="/upload">New analysis</Link>}
        </nav>
        <div className="foot">
          {user ? (
            <>
              <div style={{ marginBottom: 8 }}>
                {user.name} <span className="tag">{user.role}</span>
              </div>
              <LogoutButton />
            </>
          ) : (
            <Link href="/login">Sign in</Link>
          )}
        </div>
      </aside>
      <main className="main">
        <div className="wrap">
          <div className="topbar">
            <h1>
              {title} {crumb && <span className="crumb">{crumb}</span>}
            </h1>
          </div>
          {children}
        </div>
      </main>
    </div>
  );
}
