import Link from "next/link";
import { AuthForm } from "@/components/forms";

export default function Login() {
  return (
    <div className="wrap" style={{ maxWidth: 420 }}>
      <div className="brand" style={{ display: "flex", alignItems: "center", gap: 10, fontWeight: 700, margin: "28px 0 16px" }}>
        <span className="ball" /> Courtside
      </div>
      <div className="card">
        <h2>Sign in</h2>
        <AuthForm mode="login" />
      </div>
      <p className="hint">
        No account? <Link href="/signup">Create one</Link>
      </p>
    </div>
  );
}
