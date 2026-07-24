import Link from "next/link";
import { AuthForm } from "@/components/forms";

export default function Signup() {
  return (
    <div className="wrap" style={{ maxWidth: 420 }}>
      <div className="brand" style={{ display: "flex", alignItems: "center", gap: 10, fontWeight: 700, margin: "28px 0 16px" }}>
        <span className="ball" /> Courtside
      </div>
      <div className="card">
        <h2>Create your account</h2>
        <p className="hint">Coaches create teams and invite players; players join with a code.</p>
        <AuthForm mode="signup" />
      </div>
      <p className="hint">
        Already have an account? <Link href="/login">Sign in</Link>
      </p>
    </div>
  );
}
