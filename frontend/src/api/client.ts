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

export type ApiKeyStatus = "active" | "revoked" | "expired";

export type ApiKey = {
  id: string;
  project_id: string;
  name: string;
  key_prefix: string;
  key_last_four: string;
  secret: string | null;
  status: ApiKeyStatus;
  expires_at: string | null;
  last_used_at: string | null;
  revoked_at: string | null;
  created_at: string;
};

/** 项目当前已开通的服务额度及 UTC 月度用量。 */
export type ProjectService = {
  project_id: string;
  service_code: string;
  monthly_token_limit: number;
  status: "active" | "suspended";
  period_start: string;
  tokens_used: number;
  tokens_reserved: number;
};

/** 当前用户的服务能力、月度总额度和所有项目汇总用量。 */
export type UserServiceQuota = {
  service_code: string;
  name: string;
  models: string[];
  monthly_token_limit: number;
  allocated_tokens: number;
  available_tokens: number;
  tokens_used: number;
  tokens_reserved: number;
};

export type CreatedApiKey = ApiKey & { secret: string };

/** 管理员维护的 OpenAI 兼容上游及其网关模型映射。 */
export type LlmProviderModel = {
  id: string;
  model_code: string;
  upstream_model: string;
  status: "active" | "disabled";
};

export type LlmProvider = {
  id: string;
  name: string;
  base_url: string;
  status: "active" | "disabled";
  api_key_configured: boolean;
  last_tested_at: string | null;
  last_test_success: boolean | null;
  last_test_message: string | null;
  models: LlmProviderModel[];
};

export type LlmProviderTestResult = {
  success: boolean;
  message: string;
  tested_at: string;
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
  getProjectApiKeys: (token: string, projectId: string) =>
    request<ApiKey[]>(`/admin/v1/projects/${projectId}/keys`, {}, token),
  createProjectApiKey: (
    token: string,
    projectId: string,
    payload: { name: string; expires_at: string | null },
  ) =>
    request<CreatedApiKey>(`/admin/v1/projects/${projectId}/keys`, {
      method: "POST",
      body: JSON.stringify(payload),
    }, token),
  revokeProjectApiKey: (token: string, projectId: string, keyId: string) =>
    request<void>(`/admin/v1/projects/${projectId}/keys/${keyId}`, {
      method: "DELETE",
    }, token),
  getMyServices: (token: string) =>
    request<UserServiceQuota[]>("/v1/me/services", {}, token),
  lookupUserForQuota: (token: string, lookup: { user_id: string } | { username: string }) => {
    const params = new URLSearchParams(lookup);
    return request<UserResponse>(`/admin/v1/users/lookup?${params.toString()}`, {}, token);
  },
  getUserServicesForAdmin: (token: string, userId: string) =>
    request<UserServiceQuota[]>(`/admin/v1/users/${userId}/services`, {}, token),
  setUserServiceQuota: (token: string, userId: string, serviceCode: string, monthlyTokenLimit: number) =>
    request<UserServiceQuota>(`/admin/v1/users/${userId}/services/${serviceCode}`, {
      method: "PUT",
      body: JSON.stringify({ monthly_token_limit: monthlyTokenLimit }),
    }, token),
  getProjectServices: (token: string, projectId: string) =>
    request<ProjectService[]>(`/admin/v1/projects/${projectId}/services`, {}, token),
  applyProjectService: (
    token: string,
    projectId: string,
    payload: { service_code: string; monthly_token_limit: number },
  ) =>
    request<ProjectService>(`/admin/v1/projects/${projectId}/services`, {
      method: "POST",
      body: JSON.stringify(payload),
    }, token),
  updateProjectServiceAllocation: (
    token: string,
    projectId: string,
    serviceCode: string,
    monthlyTokenLimit: number,
  ) =>
    request<ProjectService>(`/admin/v1/projects/${projectId}/services/${serviceCode}`, {
      method: "PATCH",
      body: JSON.stringify({ monthly_token_limit: monthlyTokenLimit }),
    }, token),
  getLlmProviders: (token: string) =>
    request<LlmProvider[]>("/admin/v1/llm/providers", {}, token),
  createLlmProvider: (
    token: string,
    payload: { name: string; base_url: string; api_key: string },
  ) => request<LlmProvider>("/admin/v1/llm/providers", {
    method: "POST",
    body: JSON.stringify(payload),
  }, token),
  updateLlmProvider: (
    token: string,
    providerId: string,
    payload: { name?: string; base_url?: string; api_key?: string; status?: "active" | "disabled" },
  ) => request<LlmProvider>(`/admin/v1/llm/providers/${providerId}`, {
    method: "PATCH",
    body: JSON.stringify(payload),
  }, token),
  deleteLlmProvider: (token: string, providerId: string) =>
    request<void>(`/admin/v1/llm/providers/${providerId}`, { method: "DELETE" }, token),
  testLlmProvider: (token: string, providerId: string) =>
    request<LlmProviderTestResult>(`/admin/v1/llm/providers/${providerId}/test`, {
      method: "POST",
    }, token),
  createLlmProviderModel: (
    token: string,
    providerId: string,
    payload: { model_code: string; upstream_model: string },
  ) => request<LlmProviderModel>(`/admin/v1/llm/providers/${providerId}/models`, {
    method: "POST",
    body: JSON.stringify(payload),
  }, token),
  updateLlmProviderModel: (
    token: string,
    modelId: string,
    payload: { model_code?: string; upstream_model?: string; status?: "active" | "disabled" },
  ) => request<LlmProviderModel>(`/admin/v1/llm/models/${modelId}`, {
    method: "PATCH",
    body: JSON.stringify(payload),
  }, token),
  deleteLlmProviderModel: (token: string, modelId: string) =>
    request<void>(`/admin/v1/llm/models/${modelId}`, { method: "DELETE" }, token),
};
