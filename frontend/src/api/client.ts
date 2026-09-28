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
export type ProjectVisibility = "public" | "private";

export type Project = {
  id: string;
  name: string;
  description: string | null;
  status: ProjectStatus;
  visibility: ProjectVisibility;
  tags: string[];
  owner_id: string;
  created_at: string;
  updated_at: string;
};

export type ProjectPage = {
  items: Project[];
  total: number;
  offset: number;
  limit: number;
};

export type ProjectMemberRole = "owner" | "editor" | "viewer";

export type ProjectMember = {
  project_id: string;
  user_id: string;
  username: string;
  role: ProjectMemberRole;
  created_at: string;
  updated_at: string;
};

export type ApiError = {
  code?: string;
  message?: string;
  request_id?: string;
  detail?: string | { code?: string; message?: string };
};

export class ApiClientError extends Error {
  readonly status: number;
  readonly body: ApiError | null;

  constructor(status: number, body: ApiError | null) {
    const detailMessage = typeof body?.detail === "string" ? body.detail : body?.detail?.message;
    super(body?.message ?? detailMessage ?? `请求失败（HTTP ${status}）`);
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
  if (response.status === 204) return undefined as T;
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
  getProjects: (
    token: string,
    filters: { query?: string; tag?: string; status?: ProjectStatus; offset?: number; limit?: number } = {},
  ) => {
    const params = new URLSearchParams();
    if (filters.query) params.set("query", filters.query);
    if (filters.tag) params.set("tag", filters.tag);
    if (filters.status) params.set("status", filters.status);
    if (filters.offset !== undefined) params.set("offset", String(filters.offset));
    if (filters.limit !== undefined) params.set("limit", String(filters.limit));
    const suffix = params.size > 0 ? `?${params.toString()}` : "";
    return request<ProjectPage>(`/admin/v1/projects${suffix}`, {}, token);
  },
  getProjectTags: (token: string) => request<string[]>("/admin/v1/projects/tags", {}, token),
  getProject: (token: string, projectId: string) =>
    request<Project>(`/admin/v1/projects/${projectId}`, {}, token),
  createProject: (
    token: string,
    payload: { name: string; description?: string; visibility: ProjectVisibility; tags: string[] },
  ) =>
    request<Project>("/admin/v1/projects", {
      method: "POST",
      body: JSON.stringify(payload),
    }, token),
  updateProject: (
    token: string,
    projectId: string,
    payload: {
      name?: string;
      description?: string | null;
      status?: ProjectStatus;
      visibility?: ProjectVisibility;
      tags?: string[];
    },
  ) =>
    request<Project>(`/admin/v1/projects/${projectId}`, {
      method: "PATCH",
      body: JSON.stringify(payload),
    }, token),
  deleteProject: (token: string, projectId: string) =>
    request<void>(`/admin/v1/projects/${projectId}`, { method: "DELETE" }, token),
  getProjectMembers: (token: string, projectId: string) =>
    request<ProjectMember[]>(`/admin/v1/projects/${projectId}/members`, {}, token),
  addProjectMember: (
    token: string,
    projectId: string,
    payload: { user_id?: string; username?: string; role: Exclude<ProjectMemberRole, "owner"> },
  ) =>
    request<ProjectMember>(`/admin/v1/projects/${projectId}/members`, {
      method: "POST",
      body: JSON.stringify(payload),
    }, token),
  updateProjectMemberRole: (
    token: string,
    projectId: string,
    userId: string,
    role: Exclude<ProjectMemberRole, "owner">,
  ) =>
    request<ProjectMember>(`/admin/v1/projects/${projectId}/members/${userId}`, {
      method: "PATCH",
      body: JSON.stringify({ role }),
    }, token),
  removeProjectMember: (token: string, projectId: string, userId: string) =>
    request<void>(`/admin/v1/projects/${projectId}/members/${userId}`, {
      method: "DELETE",
    }, token),
};
