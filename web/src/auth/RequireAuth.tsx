// Route guard. Redirects to /signin when the user isn't authenticated;
// renders a quick "loading" placeholder while we resolve the cookie.

import { ReactNode } from "react";
import { Navigate, useLocation } from "react-router-dom";
import { useAuth } from "./AuthContext";

export function RequireAuth({ children }: { children: ReactNode }) {
  const { state } = useAuth();
  const location = useLocation();

  if (state.status === "loading") {
    return <p style={{ padding: "2rem" }}>Loading…</p>;
  }
  if (state.status === "signed_out") {
    return (
      <Navigate
        to="/signin"
        replace
        state={{ from: location.pathname + location.search }}
      />
    );
  }
  return <>{children}</>;
}
