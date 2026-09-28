export type HealthResponse = {
  status: "ok";
};

export type LoginRequest = {
  username: string;
  password: string;
};

export type RegisterRequest = LoginRequest;

export type TokenResponse = {
  access_token: string;
  token_type: "bearer";
  expires_in: number;
};

export type UserResponse = {
  id: string;
  username: string;
  role: "admin" | "user";
};

export type ProjectStatus = "active" | "inactive";

export type Project = {
  id: string;
  name: string;
  description: string | null;
  status: ProjectStatus;
  owner_id: string;
  created_at: string;
  updated_at: string;
};

export type ApiError = {
  code?: string;
  message?: string;
  request_id?: string;
};

export class ApiClientError extends Error {
  readonly status: number;
  readonly body: ApiError | null;

  constructor(status: number, body: ApiError | null) {
    super(body?.message ?? `请求失败（HTTP ${status}）`);
    this.name = "ApiClientError";
    this.status = status;
    this.body = body;
  }
}

async function request<T>(
  path: string,
  options: RequestInit = {},
  token?: string,
): Promise<T> {
  const response = await fetch(`/api${path}`, {
    ...options,
    headers: {
      Accept: "application/json",
      ...(options.body ? { "Content-Type": "application/json" } : {}),
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
      ...options.headers,
    },
  });
  if (!response.ok) {
    let body: ApiError | null = null;
    try {
      body = (await response.json()) as ApiError;
    } catch {
      body = null;
    }
    throw new ApiClientError(response.status, body);
  }
  return (await response.json()) as T;
}

export const apiClient = {
  getLiveness: () => request<HealthResponse>("/health/live"),
  getReadiness: () => request<HealthResponse>("/health/ready"),
  login: (payload: LoginRequest) =>
    request<TokenResponse>("/v1/auth/login", {
      method: "POST",
      body: JSON.stringify(payload),
    }),
  register: (payload: RegisterRequest) =>
    request<UserResponse>("/v1/auth/register", {
      method: "POST",
      body: JSON.stringify(payload),
    }),
  getCurrentUser: (token: string) => request<UserResponse>("/v1/auth/me", {}, token),
  logout: (token: string) => request<void>("/v1/auth/logout", { method: "POST" }, token),
  getProjects: (token: string) => request<Project[]>("/admin/v1/projects", {}, token),
  createProject: (token: string, payload: { name: string; description?: string }) =>
    request<Project>("/admin/v1/projects", {
      method: "POST",
      body: JSON.stringify(payload),
    }, token),
  updateProject: (
    token: string,
    projectId: string,
    payload: { name?: string; description?: string; status?: ProjectStatus },
  ) =>
    request<Project>(`/admin/v1/projects/${projectId}`, {
      method: "PATCH",
      body: JSON.stringify(payload),
    }, token),
};
