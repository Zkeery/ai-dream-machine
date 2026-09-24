"use client";

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useState,
} from "react";
import { clearToken, getToken, setToken } from "@/lib/api/client";
import {
  login as apiLogin,
  logout as apiLogout,
  me as fetchMe,
  type Me,
} from "@/lib/api/auth";

type AuthState = {
  user: Me | null;
  loading: boolean;
  login: (code: string) => Promise<void>;
  logout: () => Promise<void>;
};

const AuthContext = createContext<AuthState | null>(null);

export function AuthProvider({ children }: { children: React.ReactNode }) {
  const [user, setUser] = useState<Me | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let cancelled = false;
    const work: Promise<void> = getToken()
      ? fetchMe().then((u) => {
          if (!cancelled) setUser(u);
        })
      : Promise.resolve();
    work
      .catch(() => {
        clearToken();
        if (!cancelled) setUser(null);
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const login = useCallback(async (code: string) => {
    const result = await apiLogin(code);
    setToken(result.token);
    setUser(await fetchMe());
  }, []);

  const logout = useCallback(async () => {
    try {
      await apiLogout();
    } catch {
      // 忽略登出接口失败，本地仍清空
    }
    clearToken();
    setUser(null);
  }, []);

  return (
    <AuthContext.Provider value={{ user, loading, login, logout }}>
      {children}
    </AuthContext.Provider>
  );
}

export function useAuth(): AuthState {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error("useAuth 必须在 AuthProvider 内使用");
  return ctx;
}
