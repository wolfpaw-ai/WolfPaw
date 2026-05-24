// Session state for the app. We don't store anything in localStorage —
// the wp_session cookie is the source of truth, and `auth.me()` is how
// we learn whether the cookie is still valid.

import {
  createContext, ReactNode, useCallback, useContext,
  useEffect, useMemo, useState,
} from "react";
import { ApiError, auth } from "../api/client";
import type { UserMe } from "../api/types";

type AuthState =
  | { status: "loading" }
  | { status: "signed_in"; user: UserMe }
  | { status: "signed_out" };

interface AuthContextValue {
  state: AuthState;
  refresh: () => Promise<void>;
  logout: () => Promise<void>;
}

const AuthContext = createContext<AuthContextValue | null>(null);

export function AuthProvider({ children }: { children: ReactNode }) {
  const [state, setState] = useState<AuthState>({ status: "loading" });

  const refresh = useCallback(async () => {
    try {
      const user = await auth.me();
      setState({ status: "signed_in", user });
    } catch (e) {
      if (e instanceof ApiError && e.status === 401) {
        setState({ status: "signed_out" });
      } else {
        // Unknown failure — treat as signed_out so the user can recover
        // by re-signing in; log so dev sees it.
        console.error("auth.me failed", e);
        setState({ status: "signed_out" });
      }
    }
  }, []);

  const logout = useCallback(async () => {
    try {
      await auth.logout();
    } finally {
      setState({ status: "signed_out" });
    }
  }, []);

  useEffect(() => {
    refresh();
  }, [refresh]);

  const value = useMemo(() => ({ state, refresh, logout }), [
    state, refresh, logout,
  ]);

  return (
    <AuthContext.Provider value={value}>{children}</AuthContext.Provider>
  );
}

export function useAuth(): AuthContextValue {
  const ctx = useContext(AuthContext);
  if (ctx === null) {
    throw new Error("useAuth called outside AuthProvider");
  }
  return ctx;
}
