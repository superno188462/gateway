import { useCallback, useEffect, useMemo, useState } from "react";
import type { ReactNode } from "react";
import type { LoginRequest, UserResponse } from "../api/client";
import { apiClient } from "../api/client";
import { AuthContext } from "./context";

const TOKEN_KEY = "agent_gateway_access_token";

export function AuthProvider({ children }: { children: ReactNode }) {
  const [token, setToken] = useState<string | null>(() => localStorage.getItem(TOKEN_KEY));
  const [user, setUser] = useState<UserResponse | null>(null);
  const [isLoading, setIsLoading] = useState(Boolean(token));

  const clearSession = useCallback(() => {
    localStorage.removeItem(TOKEN_KEY);
    setToken(null);
    setUser(null);
  }, []);

  useEffect(() => {
    if (!token) {
      setIsLoading(false);
      return;
    }
    let active = true;
    void apiClient
      .getCurrentUser(token)
      .then((currentUser) => {
        if (active) setUser(currentUser);
      })
      .catch(() => {
        if (active) clearSession();
      })
      .finally(() => {
        if (active) setIsLoading(false);
      });
    return () => {
      active = false;
    };
  }, [clearSession, token]);

  const login = useCallback(async (payload: LoginRequest) => {
    const response = await apiClient.login(payload);
    localStorage.setItem(TOKEN_KEY, response.access_token);
    setToken(response.access_token);
    const currentUser = await apiClient.getCurrentUser(response.access_token);
    setUser(currentUser);
    setIsLoading(false);
  }, []);

  const logout = useCallback(async () => {
    const currentToken = token;
    clearSession();
    if (currentToken) {
      try {
        await apiClient.logout(currentToken);
      } catch {
        // 本地会话已清理；服务器令牌可能已经过期或被撤销。
      }
    }
  }, [clearSession, token]);

  const value = useMemo(
    () => ({ user, token, isLoading, login, logout }),
    [isLoading, login, logout, token, user],
  );
  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}
