// Reached via the magic link the backend sent. Consumes the token,
// the backend sets the session cookie, then we refresh AuthContext
// and bounce into /chat.

import { useEffect, useState } from "react";
import { Navigate, useNavigate, useSearchParams } from "react-router-dom";
import { ApiError, auth } from "../api/client";
import { useAuth } from "./AuthContext";

export function VerifyPage() {
  const [params] = useSearchParams();
  const token = params.get("token");
  const navigate = useNavigate();
  const { refresh, state } = useAuth();
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!token) {
      setError("Missing token in the URL.");
      return;
    }
    let cancelled = false;
    (async () => {
      try {
        await auth.verify(token);
        if (cancelled) return;
        await refresh();
        if (cancelled) return;
        navigate("/chat", { replace: true });
      } catch (e) {
        if (cancelled) return;
        setError(
          e instanceof ApiError ? String(e.detail) : "Couldn't verify link.",
        );
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [token, refresh, navigate]);

  if (state.status === "signed_in") {
    return <Navigate to="/chat" replace />;
  }

  return (
    <main className="centered narrow">
      <h1>Signing you in…</h1>
      {error && (
        <>
          <p className="error">{error}</p>
          <p>
            <a href="/signin">Try again</a>
          </p>
        </>
      )}
    </main>
  );
}
