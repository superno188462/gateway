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
  project_id: string | null;
  user_id: string;
  username: string;
  role: ProjectMemberRole;
  created_at: string;
  updated_at: string;
};

export type ApiKeyStatus = "active" | "revoked" | "expired";

export type ApiKey = {
  id: string;
  project_id: string | null;
  created_by_user_id: string;
  created_by_username: string;
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
  project_id: string | null;
  service_code: string;
  monthly_token_limit: number | null;
  status: "active" | "suspended";
  period_start: string;
  tokens_used: number;
  tokens_reserved: number;
};

export type ProjectResource = {
  id: string;
  project_id: string;
  resource_type: "memory" | "template";
  category: "sessions" | "profiles" | "longterm" | "system" | "user" | "assistant";
  name: string;
  content: string;
  version: number;
  created_by: string;
  updated_by: string;
  created_at: string;
  updated_at: string;
};

export type ContextProjectPage = {
  items: Array<Pick<Project, "id" | "name" | "description" | "visibility" | "owner_id">>;
  total: number;
  offset: number;
  limit: number;
};

export type ServiceProject = Pick<Project, "id" | "name" | "description" | "visibility" | "owner_id"> & {
  service_code: string;
  monthly_token_limit: number | null;
  service_status: "active" | "suspended";
  tokens_used: number;
  tokens_reserved: number;
};

export type ServiceProjectPage = {
  items: ServiceProject[];
  total: number;
  offset: number;
  limit: number;
};

export type ContextPermissions = {
  can_read_user_data: boolean;
  can_edit: boolean;
};

export type ContextSessionMessage = {
  id: string;
  sequence: number;
  role: "system" | "user" | "assistant" | "tool";
  content: string;
  metadata: Record<string, unknown>;
  expires_at: string;
};

export type ContextSessionMessages = {
  project_id: string;
  external_user_id: string;
  session_id: string;
  messages: ContextSessionMessage[];
};

export type ContextLongTermMemory = {
  id: string;
  content: string;
  tags: string[];
  metadata: Record<string, unknown>;
  version: number;
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

/** 管理员配置的 OpenAI 兼容上游；priority 越小越优先。 */
export type LlmProvider = {
  id: string;
  name: string;
  supplier_name: string;
  route_prefix: string | null;
  base_url: string;
  status: "active" | "disabled";
  priority: number;
  api_key_configured: boolean;
  api_key: string | null;
  last_tested_at: string | null;
  last_test_success: boolean | null;
  last_test_message: string | null;
};

export type LlmProviderTestResult = {
  success: boolean;
  message: string;
  tested_at: string;
};

/** 一条不含密钥、提示词和回复正文的项目操作日志。 */
export type RequestLog = {
  request_id: string;
  trace_id: string;
  event_type: "service_call" | "project_operation";
  actor_user_id: string | null;
  actor_username: string | null;
  project_id: string | null;
  api_key_id: string | null;
  project_name: string | null;
  service_code: string;
  status: "received" | "succeeded" | "failed" | "denied";
  latency_ms: number;
  error_code: string | null;
  error_message: string | null;
  description: string | null;
  created_at: string;
};

/** 调用记录的数据库页结果及筛选命中总量。 */
export type RequestLogPage = {
  items: RequestLog[];
  next_cursor: string | null;
  page: number;
  page_size: number;
  total_count: number;
  total_pages: number;
};

export type TechnicalLogEntry = {
  timestamp: string;
  level: "DEBUG" | "INFO" | "WARNING" | "ERROR" | "CRITICAL";
  source: string;
  trace_id: string;
  message: string;
};

export type TechnicalLogPage = {
  entries: TechnicalLogEntry[];
  log_file: string;
  page: number;
  page_size: number;
  total_count: number;
  total_pages: number;
};

/** 从请求日志实时聚合的 Dashboard 指标。 */
export type RequestUsageSummary = {
  start_at: string;
  end_at: string;
  request_count: number;
  succeeded_count: number;
  failed_count: number;
  denied_count: number;
  prompt_tokens: number;
  completion_tokens: number;
  total_tokens: number;
  average_latency_ms: number;
  cost_cny: null;
  by_model: Array<{ model: string; request_count: number; total_tokens: number }>;
  by_day: Array<{ day: string; request_count: number; total_tokens: number }>;
};

export type LogRetentionRun = {
  id: string;
  status: "running" | "succeeded" | "failed";
  started_at: string;
  finished_at: string | null;
  deleted_count: number;
  error_code: string | null;
};

/** 用户可见的启用模型路由组；不包含上游连接详情。 */
export type LlmProviderCatalog = {
  groups: Array<{ prefix: string | null; suppliers?: string[]; connection_count: number }>;
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
  const appBasePath = import.meta.env.BASE_URL.replace(/\/$/, "");
  const response = await fetch(`${appBasePath}/api${path}`, {
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
  getRequestUsageSummary: (
    token: string,
    filters: { startAt?: string; endAt?: string; projectId?: string } = {},
  ) => {
    const params = new URLSearchParams();
    if (filters.startAt) params.set("start_at", filters.startAt);
    if (filters.endAt) params.set("end_at", filters.endAt);
    if (filters.projectId) params.set("project_id", filters.projectId);
    const suffix = params.size ? `?${params.toString()}` : "";
    return request<RequestUsageSummary>(`/v1/usage/summary${suffix}`, {}, token);
  },
  getProjectRequestLogs: (
    token: string,
    projectId: string,
    filters: {
      startAt?: string;
      endAt?: string;
      status?: RequestLog["status"];
      serviceCode?: string;
      requestId?: string;
      page?: number;
      pageSize?: number;
    } = {},
  ) => {
    const params = new URLSearchParams();
    if (filters.startAt) params.set("start_at", filters.startAt);
    if (filters.endAt) params.set("end_at", filters.endAt);
    if (filters.status) params.set("status", filters.status);
    if (filters.serviceCode) params.set("service_code", filters.serviceCode);
    if (filters.requestId) params.set("request_id", filters.requestId);
    if (filters.page !== undefined) params.set("page", String(filters.page));
    if (filters.pageSize !== undefined) params.set("page_size", String(filters.pageSize));
    const suffix = params.size ? `?${params.toString()}` : "";
    return request<RequestLogPage>(`/v1/projects/${encodeURIComponent(projectId)}/requests${suffix}`, {}, token);
  },
  getVisibleRequestLogs: (
    token: string,
    filters: {
      startAt?: string;
      endAt?: string;
      projectId?: string;
      status?: RequestLog["status"];
      serviceCode?: string;
      requestId?: string;
      page?: number;
      pageSize?: number;
    } = {},
  ) => {
    const params = new URLSearchParams();
    if (filters.startAt) params.set("start_at", filters.startAt);
    if (filters.endAt) params.set("end_at", filters.endAt);
    if (filters.projectId) params.set("project_id", filters.projectId);
    if (filters.status) params.set("status", filters.status);
    if (filters.serviceCode) params.set("service_code", filters.serviceCode);
    if (filters.requestId) params.set("request_id", filters.requestId);
    if (filters.page !== undefined) params.set("page", String(filters.page));
    if (filters.pageSize !== undefined) params.set("page_size", String(filters.pageSize));
    const suffix = params.size ? `?${params.toString()}` : "";
    return request<RequestLogPage>(`/v1/requests${suffix}`, {}, token);
  },
  getAdminRequestLogs: (
    token: string,
    filters: {
      startAt?: string;
      endAt?: string;
      projectId?: string;
      status?: RequestLog["status"];
      serviceCode?: string;
      requestId?: string;
      page?: number;
      pageSize?: number;
    } = {},
  ) => {
    const params = new URLSearchParams();
    if (filters.startAt) params.set("start_at", filters.startAt);
    if (filters.endAt) params.set("end_at", filters.endAt);
    if (filters.projectId) params.set("project_id", filters.projectId);
    if (filters.status) params.set("status", filters.status);
    if (filters.serviceCode) params.set("service_code", filters.serviceCode);
    if (filters.requestId) params.set("request_id", filters.requestId);
    if (filters.page !== undefined) params.set("page", String(filters.page));
    if (filters.pageSize !== undefined) params.set("page_size", String(filters.pageSize));
    const suffix = params.size ? `?${params.toString()}` : "";
    return request<RequestLogPage>(`/admin/v1/requests${suffix}`, {}, token);
  },
  getProjectRequestLog: (token: string, projectId: string, requestId: string) =>
    request<RequestLog>(
      `/v1/projects/${encodeURIComponent(projectId)}/requests/${encodeURIComponent(requestId)}`,
      {},
      token,
    ),
  getVisibleRequestLog: (token: string, requestId: string) =>
    request<RequestLog>(`/v1/requests/${encodeURIComponent(requestId)}`, {}, token),
  getAdminRequestLog: (token: string, requestId: string) =>
    request<RequestLog>(`/admin/v1/requests/logs/${encodeURIComponent(requestId)}`, {}, token),
  getLogRetentionRun: (token: string) =>
    request<LogRetentionRun | null>("/admin/v1/requests/retention", {}, token),
  runLogRetention: (token: string) =>
    request<LogRetentionRun>("/admin/v1/requests/retention/run", { method: "POST" }, token),
  getTechnicalLogs: (
    token: string,
    filters: { level?: TechnicalLogEntry["level"]; traceId?: string; query?: string; page?: number; pageSize?: number } = {},
  ) => {
    const params = new URLSearchParams();
    if (filters.level) params.set("level", filters.level);
    if (filters.traceId) params.set("trace_id", filters.traceId);
    if (filters.query) params.set("query", filters.query);
    if (filters.page !== undefined) params.set("page", String(filters.page));
    if (filters.pageSize !== undefined) params.set("page_size", String(filters.pageSize));
    const suffix = params.size ? `?${params.toString()}` : "";
    return request<TechnicalLogPage>(`/admin/v1/system-logs${suffix}`, {}, token);
  },
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
  getLlmProviderCatalog: (token: string) =>
    request<LlmProviderCatalog>("/v1/llm/provider-catalog", {}, token),
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
    payload: { service_code: string; monthly_token_limit?: number },
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
  getProjectResources: (
    token: string,
    projectId: string,
    resourceType?: ProjectResource["resource_type"],
    category?: ProjectResource["category"],
  ) => {
    const params = new URLSearchParams();
    if (resourceType && category) {
      params.set("resource_type", resourceType);
      params.set("category", category);
    }
    const suffix = params.size ? `?${params.toString()}` : "";
    return request<ProjectResource[]>(`/admin/v1/projects/${encodeURIComponent(projectId)}/resources${suffix}`, {}, token);
  },
  getContextProjects: (token: string, offset = 0, limit = 20) =>
    request<ContextProjectPage>(`/admin/v1/context/projects?offset=${offset}&limit=${limit}`, {}, token),
  getServiceProjects: (token: string, serviceCode: string, offset = 0, limit = 20) =>
    request<ServiceProjectPage>(`/admin/v1/services/${encodeURIComponent(serviceCode)}/projects?offset=${offset}&limit=${limit}`, {}, token),
  getConsoleContextProfile: (token: string, projectId: string, externalUserId: string) =>
    request<{ profile: Record<string, unknown>; version: number | null }>(
      `/admin/v1/context/projects/${encodeURIComponent(projectId)}/users/${encodeURIComponent(externalUserId)}/profile`,
      {}, token,
    ),
  getContextPermissions: (token: string, projectId: string) =>
    request<ContextPermissions>(`/admin/v1/context/projects/${encodeURIComponent(projectId)}/permissions`, {}, token),
  getConsoleContextMessages: (token: string, projectId: string, externalUserId: string, sessionId: string, limit = 50) =>
    request<ContextSessionMessages>(
      `/admin/v1/context/projects/${encodeURIComponent(projectId)}/users/${encodeURIComponent(externalUserId)}/sessions/${encodeURIComponent(sessionId)}/messages?limit=${limit}`,
      {}, token,
    ),
  getConsoleContextMemories: (token: string, projectId: string, externalUserId: string, page = 1, pageSize = 50) =>
    request<{ items: ContextLongTermMemory[]; page: number; page_size: number; total: number }>(
      `/admin/v1/context/projects/${encodeURIComponent(projectId)}/users/${encodeURIComponent(externalUserId)}/memories?page=${page}&page_size=${pageSize}`,
      {}, token,
    ),
  createProjectResource: (token: string, projectId: string, payload: Pick<ProjectResource, "resource_type" | "category" | "name" | "content">) =>
    request<ProjectResource>(`/admin/v1/projects/${encodeURIComponent(projectId)}/resources`, {
      method: "POST", body: JSON.stringify(payload),
    }, token),
  updateProjectResource: (token: string, projectId: string, resourceId: string, payload: { expected_version: number; name: string; content: string }) =>
    request<ProjectResource>(`/admin/v1/projects/${encodeURIComponent(projectId)}/resources/${encodeURIComponent(resourceId)}`, {
      method: "PATCH", body: JSON.stringify(payload),
    }, token),
  deleteProjectResource: (token: string, projectId: string, resourceId: string, expectedVersion: number) =>
    request<void>(`/admin/v1/projects/${encodeURIComponent(projectId)}/resources/${encodeURIComponent(resourceId)}?expected_version=${expectedVersion}`, {
      method: "DELETE",
    }, token),
  getLlmProviders: (token: string) =>
    request<LlmProvider[]>("/admin/v1/llm/providers", {}, token),
  createLlmProvider: (
    token: string,
    payload: { name: string; supplier_name?: string; route_prefix?: string; base_url: string; api_key: string },
  ) => request<LlmProvider>("/admin/v1/llm/providers", {
    method: "POST",
    body: JSON.stringify(payload),
  }, token),
  updateLlmProvider: (
    token: string,
    providerId: string,
    payload: { name?: string; supplier_name?: string; route_prefix?: string; base_url?: string; api_key?: string; status?: "active" | "disabled"; priority?: number },
  ) => request<LlmProvider>(`/admin/v1/llm/providers/${providerId}`, {
    method: "PATCH",
    body: JSON.stringify(payload),
  }, token),
  deleteLlmProvider: (token: string, providerId: string) =>
    request<void>(`/admin/v1/llm/providers/${providerId}`, { method: "DELETE" }, token),
  testLlmProvider: (token: string, providerId: string, model: string) =>
    request<LlmProviderTestResult>(`/admin/v1/llm/providers/${providerId}/test`, {
      method: "POST",
      body: JSON.stringify({ model }),
    }, token),
};
