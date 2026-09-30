import { useCallback, useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { ApiClientError, apiClient, type ApiKey, type Project, type ProjectResource } from "../api/client";
import { useAuth } from "../auth/useAuth";

type TemplateCategory = "system" | "user" | "assistant";

function message(reason: unknown): string {
  return reason instanceof ApiClientError ? reason.message : "上下文管理操作失败，请重试。";
}

export function ProjectContextPage() {
  const { projectId = "" } = useParams<{ projectId: string }>();
  const { token } = useAuth();
  const [project, setProject] = useState<Project | null>(null);
  const [templates, setTemplates] = useState<ProjectResource[]>([]);
  const [serviceEnabled, setServiceEnabled] = useState(false);
  const [keys, setKeys] = useState<ApiKey[]>([]);
  const [selectedKeyId, setSelectedKeyId] = useState("");
  const [externalUserId, setExternalUserId] = useState("");
  const [sessionId, setSessionId] = useState("");
  const [profileText, setProfileText] = useState("{}");
  const [profileVersion, setProfileVersion] = useState<number | null>(null);
  const [shortMemories, setShortMemories] = useState<Array<{ key: string; value: unknown; version: number; expires_at: string }>>([]);
  const [memoryKey, setMemoryKey] = useState("");
  const [memoryValue, setMemoryValue] = useState("{}");
  const [longMemories, setLongMemories] = useState<Array<{ id: string; content: string; tags: string[]; metadata: Record<string, unknown>; version: number }>>([]);
  const [longMemoryDraft, setLongMemoryDraft] = useState("");
  const [editingLongMemoryId, setEditingLongMemoryId] = useState<string | null>(null);
  const [editingLongMemoryContent, setEditingLongMemoryContent] = useState("");
  const [memoryLoading, setMemoryLoading] = useState(false);
  const [category, setCategory] = useState<TemplateCategory>("system");
  const [name, setName] = useState("");
  const [content, setContent] = useState("");
  const [editing, setEditing] = useState<ProjectResource | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [success, setSuccess] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);

  const load = useCallback(async () => {
    if (!token || !projectId) return;
    setLoading(true);
    setError(null);
    try {
      const [projectValue, systemTemplates, userTemplates, assistantTemplates, services] = await Promise.all([
        apiClient.getProject(token, projectId),
        apiClient.getProjectResources(token, projectId, "template", "system"),
        apiClient.getProjectResources(token, projectId, "template", "user"),
        apiClient.getProjectResources(token, projectId, "template", "assistant"),
        apiClient.getProjectServices(token, projectId),
      ]);
      setProject(projectValue);
      setTemplates([...systemTemplates, ...userTemplates, ...assistantTemplates]);
      setServiceEnabled(services.some((service) => service.service_code === "project-context-v1" && service.status === "active"));
      try {
        const projectKeys = await apiClient.getProjectApiKeys(token, projectId);
        const usableKeys = projectKeys.filter((key) => key.status === "active" && key.secret);
        setKeys(usableKeys);
        setSelectedKeyId((current) => usableKeys.some((key) => key.id === current) ? current : (usableKeys[0]?.id ?? ""));
      } catch {
        setKeys([]);
        setSelectedKeyId("");
      }
    } catch (reason) {
      setError(message(reason));
    } finally {
      setLoading(false);
    }
  }, [projectId, token]);

  useEffect(() => { void load(); }, [load]);

  function edit(item: ProjectResource) {
    setEditing(item);
    setCategory(item.category as TemplateCategory);
    setName(item.name);
    setContent(item.content);
    setError(null);
    setSuccess(null);
  }

  function resetForm() {
    setEditing(null);
    setName("");
    setContent("");
    setCategory("system");
  }

  async function save() {
    if (!token || !projectId || !name.trim() || saving) return;
    setSaving(true);
    setError(null);
    setSuccess(null);
    try {
      if (editing) {
        await apiClient.updateProjectResource(token, projectId, editing.id, {
          expected_version: editing.version,
          name: name.trim(),
          content,
        });
        setSuccess("模板已更新。");
      } else {
        await apiClient.createProjectResource(token, projectId, {
          resource_type: "template",
          category,
          name: name.trim(),
          content,
        });
        setSuccess("模板已创建，可通过项目 API Key 读取。");
      }
      resetForm();
      await load();
    } catch (reason) {
      setError(message(reason));
    } finally {
      setSaving(false);
    }
  }

  async function remove(item: ProjectResource) {
    if (!token || !projectId || !window.confirm(`删除模板“${item.name}”？`)) return;
    setError(null);
    try {
      await apiClient.deleteProjectResource(token, projectId, item.id, item.version);
      if (editing?.id === item.id) resetForm();
      setTemplates((current) => current.filter((resource) => resource.id !== item.id));
      setSuccess("模板已删除。");
    } catch (reason) { setError(message(reason)); }
  }

  if (loading) return <section className="page-content"><div className="empty-state">正在加载项目上下文…</div></section>;
  if (!project) return <section className="page-content"><p className="form-error">{error ?? "项目不存在或无权访问。"}</p></section>;

  const baseUrl = window.location.port === "5173"
    ? "/v1/context"
    : `${window.location.origin}/v1/context`;
  const selectedKey = keys.find((key) => key.id === selectedKeyId);

  async function contextRequest<T>(path: string, init: RequestInit = {}): Promise<T> {
    if (!selectedKey?.secret) throw new Error("请先创建并选择一把有效项目 API Key。");
    const response = await fetch(`${baseUrl}${path}`, {
      ...init,
      headers: {
        Accept: "application/json",
        ...(init.body ? { "Content-Type": "application/json" } : {}),
        Authorization: `Bearer ${selectedKey.secret}`,
        ...init.headers,
      },
    });
    if (!response.ok) {
      let body: { detail?: { message?: string }; error?: { message?: string } } = {};
      try { body = await response.json() as typeof body; } catch { /* use status text */ }
      throw new Error(body.detail?.message ?? body.error?.message ?? `请求失败（HTTP ${response.status}）`);
    }
    if (response.status === 204) return undefined as T;
    return await response.json() as T;
  }

  async function loadUserContext() {
    if (!externalUserId.trim()) {
      setError("请输入外部用户 ID。");
      return;
    }
    setMemoryLoading(true);
    setError(null);
    try {
      const userId = encodeURIComponent(externalUserId.trim());
      const session = encodeURIComponent(sessionId.trim());
      const [profile, short, long] = await Promise.all([
        contextRequest<{ profile: Record<string, unknown>; version: number | null }>(`/users/${userId}/profile`),
        sessionId.trim() ? contextRequest<typeof shortMemories>(`/users/${userId}/sessions/${session}/memories`) : Promise.resolve([] as typeof shortMemories),
        contextRequest<{ items: typeof longMemories }>(`/users/${userId}/memories?page=1&page_size=50`),
      ]);
      setProfileText(JSON.stringify(profile.profile, null, 2));
      setProfileVersion(profile.version);
      setShortMemories(short);
      setLongMemories(long.items);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "上下文读取失败。");
    } finally {
      setMemoryLoading(false);
    }
  }

  async function saveProfile() {
    if (!externalUserId.trim()) return;
    try {
      const profile = JSON.parse(profileText) as Record<string, unknown>;
      const saved = await contextRequest<{ profile: Record<string, unknown>; version: number }>(
        `/users/${encodeURIComponent(externalUserId.trim())}/profile`,
        { method: "PUT", body: JSON.stringify({ profile, expected_version: profileVersion }) },
      );
      setProfileText(JSON.stringify(saved.profile, null, 2));
      setProfileVersion(saved.version);
      setSuccess("用户画像已保存。");
      await loadUserContext();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "画像 JSON 无效或保存失败。");
    }
  }

  async function saveShortMemory() {
    if (!externalUserId.trim() || !sessionId.trim() || !memoryKey.trim()) return;
    try {
      const value = JSON.parse(memoryValue) as unknown;
      await contextRequest(
        `/users/${encodeURIComponent(externalUserId.trim())}/sessions/${encodeURIComponent(sessionId.trim())}/memories/${encodeURIComponent(memoryKey.trim())}`,
        { method: "PUT", body: JSON.stringify({ value, ttl_seconds: 86400 }) },
      );
      setMemoryKey("");
      setSuccess("短期记忆已保存，有效期 24 小时。");
      await loadUserContext();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "记忆 JSON 无效或保存失败。");
    }
  }

  async function removeShortMemory(key: string) {
    try {
      await contextRequest(
        `/users/${encodeURIComponent(externalUserId.trim())}/sessions/${encodeURIComponent(sessionId.trim())}/memories/${encodeURIComponent(key)}`,
        { method: "DELETE" },
      );
      setShortMemories((items) => items.filter((item) => item.key !== key));
    } catch (reason) { setError(reason instanceof Error ? reason.message : "删除短期记忆失败。"); }
  }

  async function createLongMemory() {
    if (!externalUserId.trim() || !longMemoryDraft.trim()) return;
    try {
      const item = await contextRequest<(typeof longMemories)[number]>(
        `/users/${encodeURIComponent(externalUserId.trim())}/memories`,
        { method: "POST", body: JSON.stringify({ content: longMemoryDraft, tags: [], metadata: {} }) },
      );
      setLongMemories((items) => [item, ...items]);
      setLongMemoryDraft("");
      setSuccess("长期记忆已保存。");
    } catch (reason) { setError(reason instanceof Error ? reason.message : "创建长期记忆失败。"); }
  }

  async function removeLongMemory(item: (typeof longMemories)[number]) {
    try {
      await contextRequest(
        `/users/${encodeURIComponent(externalUserId.trim())}/memories/${item.id}?expected_version=${item.version}`,
        { method: "DELETE" },
      );
      setLongMemories((items) => items.filter((entry) => entry.id !== item.id));
    } catch (reason) { setError(reason instanceof Error ? reason.message : "删除长期记忆失败。"); }
  }

  async function saveLongMemory(item: (typeof longMemories)[number]) {
    try {
      const updated = await contextRequest<(typeof longMemories)[number]>(
        `/users/${encodeURIComponent(externalUserId.trim())}/memories/${item.id}`,
        { method: "PATCH", body: JSON.stringify({ content: editingLongMemoryContent, tags: item.tags, metadata: item.metadata, expected_version: item.version }) },
      );
      setLongMemories((items) => items.map((entry) => entry.id === item.id ? updated : entry));
      setEditingLongMemoryId(null);
      setSuccess("长期记忆已更新。");
    } catch (reason) { setError(reason instanceof Error ? reason.message : "更新长期记忆失败。"); }
  }
  return (
    <section className="page-content" aria-labelledby="context-title">
      <Link className="back-link" to={`/projects/${projectId}`}>← 返回项目</Link>
      <div className="intro-row">
        <div><p className="page-kicker">{project.name} · 项目服务</p><h2 id="context-title">上下文管理</h2>
          <p className="page-description">维护此项目的提示词模板，并为接入应用提供项目级短期记忆、长期记忆和用户画像 API。</p></div>
        <button className="secondary-button" onClick={() => void load()} type="button">刷新</button>
      </div>
      {error && <p className="form-error" role="alert">{error}</p>}
      {success && <p className="form-success" role="status">{success}</p>}

      <section className="project-detail-panel" aria-labelledby="context-subscription-title">
        <h3 id="context-subscription-title">服务开通与隔离</h3>
        <p className={serviceEnabled ? "form-success" : "service-quota-warning"} role="status">
          {serviceEnabled ? "项目上下文服务已开通，运行时 API Key 可以访问。" : "项目上下文服务尚未开通；请项目 owner 在项目详情中申请。"}
        </p>
        <p>此服务需要项目 owner 在项目详情页申请开通。它不消耗 LLM、Embedding、TTS 或 ASR 的 token 额度。每个请求通过项目 API Key 自动确定项目，所有模板和记忆都按项目隔离；用户 ID、会话 ID 由你的应用传入。</p>
        <div className="project-detail-shortcuts"><Link className="secondary-button" to={`/projects/${projectId}/keys`}>查看项目 API Key →</Link>
          <code>{baseUrl}</code></div>
      </section>

      <section className="project-detail-panel" aria-labelledby="context-template-title">
        <div className="service-allocation-heading"><div><h3 id="context-template-title">提示词模板</h3><p>模板属于项目上下文服务，名字在同一分类内唯一；更新使用版本号避免覆盖他人修改。</p></div><span>{templates.length} 个模板</span></div>
        <div className="member-list">
          {templates.length === 0 ? <p className="member-empty">还没有模板。创建后，接入应用可以按分类和名称读取。</p> : templates.map((item) => (
            <div className="member-row" key={item.id}>
              <div className="member-identity"><strong>{item.name}</strong><span>{item.category} · v{item.version}</span></div>
              <button className="secondary-button" onClick={() => edit(item)} type="button">编辑</button>
              <button className="danger-button" onClick={() => void remove(item)} type="button">删除</button>
            </div>
          ))}
        </div>
        <div className="context-template-form">
          <h4>{editing ? `编辑模板：${editing.name}` : "新建模板"}</h4>
          <div className="project-form">
            <select aria-label="模板分类" disabled={Boolean(editing)} onChange={(event) => setCategory(event.target.value as TemplateCategory)} value={category}>
              <option value="system">System</option><option value="user">User</option><option value="assistant">Assistant</option>
            </select>
            <input aria-label="模板名称" maxLength={200} onChange={(event) => setName(event.target.value)} placeholder="例如：support-agent" value={name} />
          </div>
          <textarea aria-label="模板正文" maxLength={100000} onChange={(event) => setContent(event.target.value)} placeholder="输入模板正文；不要放置供应商密钥等凭据。" rows={8} value={content} />
          <div className="project-detail-actions"><button className="primary-button" disabled={!name.trim() || saving} onClick={() => void save()} type="button">{saving ? "保存中…" : editing ? "保存模板" : "创建模板"}</button>{editing && <button className="secondary-button" onClick={resetForm} type="button">取消编辑</button>}</div>
        </div>
      </section>

      <section className="project-detail-panel" aria-labelledby="context-api-title">
        <h3 id="context-api-title">记忆与画像 API</h3>
        <p>短期记忆支持按 key 原子覆盖和 TTL；长期记忆支持分页、标签和版本更新；用户画像使用 JSON 整体替换并校验版本。所有这些请求同样通过项目 API Key 授权。</p>
        <div className="context-api-list">
          <code>GET /v1/context/templates</code>
          <code>GET /v1/context/users/&#123;external_user_id&#125;/sessions/&#123;session_id&#125;/memories</code>
          <code>PUT /v1/context/users/&#123;external_user_id&#125;/sessions/&#123;session_id&#125;/memories/&#123;key&#125;</code>
          <code>GET · POST /v1/context/users/&#123;external_user_id&#125;/memories</code>
          <code>GET · PUT /v1/context/users/&#123;external_user_id&#125;/profile</code>
        </div>
        <pre className="context-example"><code>{`curl ${window.location.origin}${baseUrl}/users/user-123/profile \\\n  -H "Authorization: Bearer $PROJECT_API_KEY"`}</code></pre>
        <p className="member-empty">API 文档：启动后端后打开 /docs，查看 Project Context Gateway。调用日志记录成功/失败和服务码，不保存上下文正文。</p>
      </section>

      <section className="project-detail-panel" aria-labelledby="context-records-title">
        <div className="service-allocation-heading"><div><h3 id="context-records-title">查看与维护用户上下文</h3>
          <p>此控制台使用你有权查看的项目 API Key 调用相同网关接口。Key 仅在本页内存使用，不会写入浏览器存储。</p></div></div>
        {!serviceEnabled ? <p className="service-quota-warning">请先开通项目上下文服务，再读取或写入记录。</p> : (
          <>
            <div className="project-form context-user-lookup">
              <label><span>项目 API Key</span><select aria-label="项目 API Key" onChange={(event) => setSelectedKeyId(event.target.value)} value={selectedKeyId}>
                {keys.map((key) => <option key={key.id} value={key.id}>{key.name} · {key.key_prefix}…{key.key_last_four}</option>)}
              </select></label>
              <label><span>外部用户 ID</span><input onChange={(event) => setExternalUserId(event.target.value)} placeholder="你的应用中的用户唯一 ID" value={externalUserId} /></label>
              <label><span>会话 ID（短期记忆需要）</span><input onChange={(event) => setSessionId(event.target.value)} placeholder="短期记忆所属会话，可选" value={sessionId} /></label>
              <button className="primary-button" disabled={!selectedKey?.secret || memoryLoading} onClick={() => void loadUserContext()} type="button">{memoryLoading ? "读取中…" : "读取上下文"}</button>
            </div>
            {keys.length === 0 && <p className="member-empty">没有可用项目 Key，请先在项目 API Key 页面创建。</p>}
            <div className="context-memory-grid">
              <div className="context-memory-card"><h4>用户画像 {profileVersion ? `· v${profileVersion}` : "· 尚未创建"}</h4>
                <textarea aria-label="用户画像 JSON" onChange={(event) => setProfileText(event.target.value)} rows={9} value={profileText} />
                <button className="primary-button" disabled={!selectedKey?.secret || !externalUserId.trim()} onClick={() => void saveProfile()} type="button">保存画像</button>
              </div>
              <div className="context-memory-card"><h4>短期记忆 · {shortMemories.length} 条</h4>
                <div className="project-form"><input aria-label="短期记忆键" onChange={(event) => setMemoryKey(event.target.value)} placeholder="记忆键" value={memoryKey} /><input aria-label="短期记忆 JSON 值" onChange={(event) => setMemoryValue(event.target.value)} placeholder='JSON 值，例如 {"step": 1}' value={memoryValue} /><button className="secondary-button" disabled={!memoryKey.trim() || !sessionId.trim()} onClick={() => void saveShortMemory()} type="button">写入</button></div>
                <div className="context-api-list">{shortMemories.map((item) => <div className="context-memory-item" key={item.key}><code>{item.key}: {JSON.stringify(item.value)}</code><button className="danger-button" onClick={() => void removeShortMemory(item.key)} type="button">删除</button></div>)}</div>
              </div>
              <div className="context-memory-card context-memory-wide"><h4>长期记忆 · {longMemories.length} 条</h4>
                <div className="project-form"><input aria-label="新增长期记忆" onChange={(event) => setLongMemoryDraft(event.target.value)} placeholder="输入需要长期保留的事实或偏好" value={longMemoryDraft} /><button className="secondary-button" disabled={!longMemoryDraft.trim()} onClick={() => void createLongMemory()} type="button">新增记忆</button></div>
                <div className="member-list">{longMemories.map((item) => <div className="member-row" key={item.id}><div className="member-identity">{editingLongMemoryId === item.id ? <textarea aria-label="编辑长期记忆" onChange={(event) => setEditingLongMemoryContent(event.target.value)} rows={3} value={editingLongMemoryContent} /> : <span>{item.content}</span>}<small>{item.tags.join(" · ") || "无标签"} · v{item.version}</small></div>{editingLongMemoryId === item.id ? <button className="secondary-button" onClick={() => void saveLongMemory(item)} type="button">保存</button> : <button className="secondary-button" onClick={() => { setEditingLongMemoryId(item.id); setEditingLongMemoryContent(item.content); }} type="button">编辑</button>}<button className="danger-button" onClick={() => void removeLongMemory(item)} type="button">删除</button></div>)}</div>
              </div>
            </div>
          </>
        )}
      </section>
    </section>
  );
}
