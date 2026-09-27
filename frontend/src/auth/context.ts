import { createContext } from "react";
import type { LoginRequest, UserResponse } from "../api/client";

export type AuthContextValue = {
  user: UserResponse | null;
  token: string | null;
  isLoading: boolean;
  login: (payload: LoginRequest) => Promise<void>;
  logout: () => Promise<void>;
};

export const AuthContext = createContext<AuthContextValue | null>(null);
