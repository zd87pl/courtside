import Link from "next/link";
import { redirect } from "next/navigation";
import { getUser } from "@/lib/auth";

export const dynamic = "force-dynamic";

export default async function Landing() {
  let signedIn = false;
  try {
    signedIn = (await getUser()) !== null;
  } catch {
    // no DB configured yet: still render the landing page
  }
  if (signedIn) redirect("/dashboard");
  return (
    <div className="wrap">
      <div className="topbar" style={{ marginTop: 18 }}>
        <div className="brand" style={{ fontSize: 18, display: "flex", alignItems: "center", gap: 10, fontWeight: 700 }}>
          <span className="ball" /> Courtside
        </div>
        <div>
          <Link className="btn" href="/login">Sign in</Link>{" "}
          <Link className="btn btn-pri" href="/signup">Get started</Link>
        </div>
      </div>
      <div className="hero">
        <div className="eyebrow">Sports intelligence for teams</div>
        <h1>Turn raw match video into a coach-ready scouting report.</h1>
        <p>
          Courtside finds the rallies, reads every stroke with a vision-language model, and writes a
          prioritized coaching report - with flagged moments your players can actually learn from.
          Coaches create teams, players join with a code, every session is saved to the roster.
        </p>
        <p className="hint" style={{ maxWidth: 560 }}>
          Your video never uploads: the browser extracts the key frames locally and only those go to
          the analysis API.
        </p>
        <div style={{ marginTop: 18 }}>
          <Link className="btn btn-pri" href="/signup">Create a coach account</Link>{" "}
          <Link className="btn" href="/signup">I&apos;m a player</Link>
        </div>
      </div>
    </div>
  );
}
