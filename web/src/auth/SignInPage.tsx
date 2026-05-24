// Magic-link sign-in. The backend's `console` email backend (default
// in dev) prints the verify URL to logs — for hosted/SES it lands in
// the user's inbox. Either way, clicking the link arrives at
// /signin/verify?token=... and the VerifyPage finishes the flow.

import { FormEvent, useState } from "react";
import { Navigate } from "react-router-dom";
import { ApiError, auth } from "../api/client";
import { useAuth } from "./AuthContext";

export function SignInPage() {
  const { state } = useAuth();
  const [email, setEmail] = useState("");
  const [submitted, setSubmitted] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  if (state.status === "signed_in") {
    return <Navigate to="/chat" replace />;
  }

  async function onSubmit(e: FormEvent) {
    e.preventDefault();
    setError(null);
    setBusy(true);
    try {
      await auth.requestMagicLink(email);
      setSubmitted(true);
    } catch (e) {
      setError(
        e instanceof ApiError ? String(e.detail) : "Couldn't send link.",
      );
    } finally {
      setBusy(false);
    }
  }

  if (submitted) {
    return (
      <main className="centered narrow">
        <h1>Check your email</h1>
        <p>
          A sign-in link is on its way to <b>{email}</b>. The link is valid
          for 15 minutes and can be used once.
        </p>
        <p className="hint">
          In dev with the <code>console</code> email backend, look for
          the link in the backend's stdout instead.
        </p>
      </main>
    );
  }

  return (
    <main className="centered narrow">
      <h1>Sign in to Wolfpaw</h1>
      <form onSubmit={onSubmit}>
        <label>
          Email
          <input
            type="email"
            required
            autoFocus
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            disabled={busy}
          />
        </label>
        <button type="submit" disabled={busy || !email}>
          {busy ? "Sending…" : "Email me a sign-in link"}
        </button>
        {error && <p className="error">{error}</p>}
      </form>
    </main>
  );
}
