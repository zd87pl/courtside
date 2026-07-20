"use client";

import { useRouter } from "next/navigation";
import { useState } from "react";

async function post(url: string, body: unknown): Promise<Record<string, unknown>> {
  const res = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const data = (await res.json().catch(() => ({}))) as Record<string, unknown>;
  if (!res.ok) throw new Error(String(data.error ?? `request failed (${res.status})`));
  return data;
}

function useSubmit(fn: () => Promise<void>) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError("");
    try {
      await fn();
    } catch (err) {
      setError(err instanceof Error ? err.message : "something went wrong");
    } finally {
      setBusy(false);
    }
  };
  return { busy, error, submit };
}

export function AuthForm({ mode }: { mode: "login" | "signup" }) {
  const router = useRouter();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [name, setName] = useState("");
  const [role, setRole] = useState<"coach" | "player">("coach");
  const { busy, error, submit } = useSubmit(async () => {
    await post(`/api/auth/${mode}`, mode === "signup" ? { email, password, name, role } : { email, password });
    router.push("/dashboard");
    router.refresh();
  });
  return (
    <form onSubmit={submit}>
      {mode === "signup" && (
        <>
          <label className="f">Name</label>
          <input value={name} onChange={(e) => setName(e.target.value)} required maxLength={120} />
          <label className="f">I am a</label>
          <select value={role} onChange={(e) => setRole(e.target.value as "coach" | "player")}>
            <option value="coach">Coach</option>
            <option value="player">Player</option>
          </select>
        </>
      )}
      <label className="f">Email</label>
      <input type="email" value={email} onChange={(e) => setEmail(e.target.value)} required />
      <label className="f">Password</label>
      <input type="password" value={password} onChange={(e) => setPassword(e.target.value)} required minLength={8} />
      <div style={{ marginTop: 14 }}>
        <button className="btn-pri" disabled={busy} type="submit">
          {busy ? "..." : mode === "signup" ? "Create account" : "Sign in"}
        </button>
      </div>
      {error && <p className="err">{error}</p>}
    </form>
  );
}

export function LogoutButton() {
  const router = useRouter();
  return (
    <button
      onClick={async () => {
        await post("/api/auth/logout", {});
        router.push("/");
        router.refresh();
      }}
    >
      Sign out
    </button>
  );
}

export function CreateTeamForm() {
  const router = useRouter();
  const [name, setName] = useState("");
  const [school, setSchool] = useState("");
  const { busy, error, submit } = useSubmit(async () => {
    await post("/api/teams", { name, school });
    setName("");
    setSchool("");
    router.refresh();
  });
  return (
    <form onSubmit={submit} className="row">
      <div>
        <label className="f">Team name</label>
        <input value={name} onChange={(e) => setName(e.target.value)} required maxLength={120} />
      </div>
      <div>
        <label className="f">School</label>
        <input value={school} onChange={(e) => setSchool(e.target.value)} maxLength={120} />
      </div>
      <div style={{ flex: "0 0 auto" }}>
        <button className="btn-pri" disabled={busy} type="submit">Create team</button>
      </div>
      {error && <p className="err">{error}</p>}
    </form>
  );
}

export function JoinTeamForm() {
  const router = useRouter();
  const [code, setCode] = useState("");
  const [joined, setJoined] = useState("");
  const { busy, error, submit } = useSubmit(async () => {
    const r = await post("/api/teams/join", { code });
    setJoined(String(r.name ?? "team"));
    setCode("");
    router.refresh();
  });
  return (
    <form onSubmit={submit} className="row">
      <div>
        <label className="f">Invite code from your coach</label>
        <input value={code} onChange={(e) => setCode(e.target.value.toUpperCase())} required maxLength={12} />
      </div>
      <div style={{ flex: "0 0 auto" }}>
        <button className="btn-pri" disabled={busy} type="submit">Join team</button>
      </div>
      {error && <p className="err">{error}</p>}
      {joined && <p className="okmsg">Joined {joined}.</p>}
    </form>
  );
}
