import { useCallback, useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { ApiClientError, apiClient, type ApiKey, type ContextLongTermMemory, type ContextSessionMessage, type Project, type ProjectResource } from "../api/client";
import { useAuth } from "../auth/useAuth";

type Section = "templates" | "short" | "long" | "profile";
type Category = "system" | "user" | "assistant";
const titles: Record<Section, string> = { templates: "提示词模板", short: "短期记忆", long: "长期记忆", profile: "用户画像" };

function errorMessage(error: unknown): string {
  return error instanceof ApiClientError ? error.message : error instanceof Error ? error.message : "上下文操作失败，请重试。";
}

/** 独立管理某一类项目上下文；控制台读取采用登录态权限，运行时写入仍使用项目 API Key。 */
export function ContextSectionPage() {
  const { projectId = "", section: routeSection = "templates" } = useParams<{ projectId: string; section: Section }>();
  const section = routeSection in titles ? routeSection as Section : "templates";
  const { token } = useAuth();
  const [project, setProject] = useState<Project | null>(null);
  const [canReadUserData, setCanReadUserData] = useState(false);
  const [canManageContext, setCanManageContext] = useState(false);
  const [templates, setTemplates] = useState<ProjectResource[]>([]);
  const [keys, setKeys] = useState<ApiKey[]>([]);
  const [selectedKeyId, setSelectedKeyId] = useState("");
  const [externalUserId, setExternalUserId] = useState("");
  const [sessionId, setSessionId] = useState("");
  const [profileText, setProfileText] = useState("{}");
  const [profileVersion, setProfileVersion] = useState<number | null>(null);
  const [messages, setMessages] = useState<ContextSessionMessage[]>([]);
  const [messageRole, setMessageRole] = useState<ContextSessionMessage["role"]>("user");
  const [messageDraft, setMessageDraft] = useState("");
  const [longMemories, setLongMemories] = useState<ContextLongTermMemory[]>([]);
  const [longMemoryDraft, setLongMemoryDraft] = useState("");
  const [editingMemoryId, setEditingMemoryId] = useState<string | null>(null);
  const [editingMemoryText, setEditingMemoryText] = useState("");
  const [templateCategory, setTemplateCategory] = useState<Category>("system");
  const [templateName, setTemplateName] = useState("");
  const [templateContent, setTemplateContent] = useState("");
  const [editingTemplate, setEditingTemplate] = useState<ProjectResource | null>(null);
  const [viewingTemplate, setViewingTemplate] = useState<ProjectResource | null>(null);
  const [busy, setBusy] = useState(false);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const key = keys.find((item) => item.id === selectedKeyId);
  const canEdit = canManageContext;
  const canRead = canReadUserData;
  // 本地开发时走 Vite 同源代理，避免浏览器跨域预检失败；生产环境走当前站点的 API。
  const appBasePath = import.meta.env.BASE_URL.replace(/\/$/, "");
  const baseUrl = `${window.location.origin}${appBasePath}/v1/context`;

  const load = useCallback(async () => {
    if (!token || !projectId) return;
    setLoading(true);
    setError(null);
    try {
      const [projectValue, system, userTemplates, assistant, permissions] = await Promise.all([
        apiClient.getProject(token, projectId),
        apiClient.getProjectResources(token, projectId, "template", "system"),
        apiClient.getProjectResources(token, projectId, "template", "user"),
        apiClient.getProjectResources(token, projectId, "template", "assistant"),
        apiClient.getContextPermissions(token, projectId),
      ]);
      setProject(projectValue);
      setTemplates([...system, ...userTemplates, ...assistant]);
      setCanReadUserData(permissions.can_read_user_data);
      setCanManageContext(permissions.can_edit);
      try {
        const projectKeys = await apiClient.getProjectApiKeys(token, projectId);
        const activeKeys = projectKeys.filter((item) => item.status === "active" && item.secret);
        setKeys(activeKeys);
        setSelectedKeyId((current) => activeKeys.some((item) => item.id === current) ? current : activeKeys[0]?.id ?? "");
      } catch {
        setKeys([]);
        setSelectedKeyId("");
      }
    } catch (reason) {
      setError(errorMessage(reason));
    } finally {
      setLoading(false);
    }
  }, [projectId, token]);

  useEffect(() => { void load(); }, [load]);

  async function requestContext<T>(path: string, init: RequestInit = {}): Promise<T> {
    if (!key?.secret) throw new Error("只有项目 owner/editor 可以通过项目 API Key 维护上下文。");
    const response = await fetch(`${baseUrl}${path}`, {
      ...init,
      headers: { Accept: "application/json", ...(init.body ? { "Content-Type": "application/json" } : {}), Authorization: `Bearer ${key.secret}`, ...init.headers },
    });
    if (!response.ok) {
      let body: { detail?: { message?: string }; error?: { message?: string } } = {};
      try { body = await response.json() as typeof body; } catch { /* 以 HTTP 状态作为回退 */ }
      throw new Error(body.detail?.message ?? body.error?.message ?? `请求失败（HTTP ${response.status}）`);
    }
    if (response.status === 204) return undefined as T;
    return await response.json() as T;
  }

  async function readRecords() {
    if (!token || !externalUserId.trim()) { setError("请输入外部用户 ID。"); return; }
    if (section === "short" && !sessionId.trim()) { setError("请输入会话 ID。"); return; }
    setBusy(true); setError(null); setNotice(null);
    try {
      if (section === "profile") {
        const result = await apiClient.getConsoleContextProfile(token, projectId, externalUserId.trim());
        setProfileText(JSON.stringify(result.profile, null, 2)); setProfileVersion(result.version);
      } else if (section === "short") {
        const result = await apiClient.getConsoleContextMessages(token, projectId, externalUserId.trim(), sessionId.trim());
        setMessages(result.messages);
      } else if (section === "long") {
        const result = await apiClient.getConsoleContextMemories(token, projectId, externalUserId.trim());
        setLongMemories(result.items);
      }
    } catch (reason) { setError(errorMessage(reason)); }
    finally { setBusy(false); }
  }

  async function saveProfile() {
    if (!externalUserId.trim()) return;
    setBusy(true); setError(null);
    try {
      const profile = JSON.parse(profileText) as Record<string, unknown>;
      const result = await requestContext<{ profile: Record<string, unknown>; version: number }>(`/users/${encodeURIComponent(externalUserId.trim())}/profile`, {
        method: "PUT", body: JSON.stringify({ profile, expected_version: profileVersion }),
      });
      setProfileText(JSON.stringify(result.profile, null, 2)); setProfileVersion(result.version); setNotice("画像已保存。");
    } catch (reason) { setError(errorMessage(reason)); }
    finally { setBusy(false); }
  }

  async function appendMessage() {
    if (!sessionId.trim() || !messageDraft.trim()) return;
    setBusy(true); setError(null);
    try {
      await requestContext(`/users/${encodeURIComponent(externalUserId.trim())}/sessions/${encodeURIComponent(sessionId.trim())}/messages`, {
        method: "POST", body: JSON.stringify({ messages: [{ role: messageRole, content: messageDraft }], ttl_seconds: 86400 }),
      });
      setMessageDraft(""); setNotice("消息已追加，有效期 24 小时。"); await readRecords();
    } catch (reason) { setError(errorMessage(reason)); }
    finally { setBusy(false); }
  }

  async function clearSessionMessages() {
    if (!externalUserId.trim() || !sessionId.trim() || !window.confirm("清除该用户在此会话中的全部短期消息？")) return;
    setBusy(true); setError(null);
    try {
      await requestContext(`/users/${encodeURIComponent(externalUserId.trim())}/sessions/${encodeURIComponent(sessionId.trim())}/messages`, { method: "DELETE" });
      setMessages([]); setNotice("会话短期消息已清除。");
    } catch (reason) { setError(errorMessage(reason)); }
    finally { setBusy(false); }
  }

  async function createLongMemory() {
    if (!longMemoryDraft.trim()) return;
    setBusy(true); setError(null);
    try {
      const item = await requestContext<ContextLongTermMemory>(`/users/${encodeURIComponent(externalUserId.trim())}/memories`, {
        method: "POST", body: JSON.stringify({ content: longMemoryDraft.trim(), tags: [], metadata: {} }),
      });
      setLongMemories((items) => [item, ...items]); setLongMemoryDraft(""); setNotice("长期记忆已保存。");
    } catch (reason) { setError(errorMessage(reason)); }
    finally { setBusy(false); }
  }

  async function updateLongMemory(item: ContextLongTermMemory) {
    setBusy(true); setError(null);
    try {
      const updated = await requestContext<ContextLongTermMemory>(`/users/${encodeURIComponent(externalUserId.trim())}/memories/${item.id}`, {
        method: "PATCH", body: JSON.stringify({ content: editingMemoryText, tags: item.tags, metadata: item.metadata, expected_version: item.version }),
      });
      setLongMemories((items) => items.map((entry) => entry.id === item.id ? updated : entry)); setEditingMemoryId(null); setNotice("长期记忆已更新。");
    } catch (reason) { setError(errorMessage(reason)); }
    finally { setBusy(false); }
  }

  async function deleteLongMemory(item: ContextLongTermMemory) {
    if (!window.confirm("删除这条长期记忆？")) return;
    setBusy(true); setError(null);
    try {
      await requestContext(`/users/${encodeURIComponent(externalUserId.trim())}/memories/${item.id}?expected_version=${item.version}`, { method: "DELETE" });
      setLongMemories((items) => items.filter((entry) => entry.id !== item.id)); setNotice("长期记忆已删除。");
    } catch (reason) { setError(errorMessage(reason)); }
    finally { setBusy(false); }
  }

  async function saveTemplate() {
    if (!token || !templateName.trim()) return;
    if (!key?.secret) { setError("请选择一个有效的项目 API Key；模板写入通过 API Key 鉴权。"); return; }
    setBusy(true); setError(null);
    try {
      if (editingTemplate) {
        await requestContext<ProjectResource>(`/templates/${editingTemplate.id}`, { method: "PATCH", body: JSON.stringify({ expected_version: editingTemplate.version, name: templateName.trim(), content: templateContent }) });
      } else {
        await requestContext<ProjectResource>("/templates", { method: "POST", body: JSON.stringify({ category: templateCategory, name: templateName.trim(), content: templateContent }) });
      }
      setEditingTemplate(null); setTemplateName(""); setTemplateContent(""); setNotice(editingTemplate ? "模板已更新。" : "模板已创建。"); await load();
    } catch (reason) { setError(errorMessage(reason)); }
    finally { setBusy(false); }
  }

  async function deleteTemplate(item: ProjectResource) {
    if (!token || !window.confirm(`删除提示词模板“${item.name}”？`)) return;
    if (!key?.secret) { setError("请选择一个有效的项目 API Key；模板写入通过 API Key 鉴权。"); return; }
    setBusy(true); setError(null);
    try {
      await requestContext<void>(`/templates/${item.id}?expected_version=${item.version}`, { method: "DELETE" });
      setTemplates((items) => items.filter((entry) => entry.id !== item.id)); setNotice("模板已删除。");
    } catch (reason) { setError(errorMessage(reason)); }
    finally { setBusy(false); }
  }

  if (loading) return <section className="page-content"><div className="empty-state">正在加载上下文服务…</div></section>;
  if (!project) return <section className="page-content"><p className="form-error">{error ?? "项目不存在或无权访问。"}</p></section>;

  return (
    <section className="page-content" aria-labelledby="context-section-title">
      <Link className="back-link" to="/services/context">← 返回上下文项目</Link>
      <div className="intro-row"><div><p className="page-kicker">服务 · {project.name}</p><h2 id="context-section-title">{titles[section]}</h2>
        <p className="page-description">{section === "templates" ? "项目内命名的提示词文本，可按模板 ID 或名称读取。" : section === "short" ? "保存当前会话的有序 messages，每条消息可设置过期时间。" : section === "long" ? "保存跨会话仍有价值的文本记忆，可附带标签和元数据。" : "保存由接入项目自行定义的 JSON 画像，网关不规定字段。"}</p></div>
        <button className="secondary-button" disabled={busy} onClick={() => void load()} type="button">刷新</button></div>
      <nav className="context-page-tabs" aria-label="上下文页面">{(Object.keys(titles) as Section[]).map((item) => <Link className={item === section ? "context-page-tab context-page-tab-active" : "context-page-tab"} key={item} to={`/services/context/${projectId}/${item}`}>{titles[item]}</Link>)}</nav>
      {error && <p className="form-error" role="alert">{error}</p>}{notice && <p className="form-success" role="status">{notice}</p>}
      {section === "templates" && <section className="project-detail-panel"><div className="service-allocation-heading"><div><h3>模板列表</h3><p>编辑需要项目 owner/editor 权限。正文按原样返回；变量由接入应用填充。</p></div><span>{templates.length} 个模板</span></div>
        {templates.length === 0 ? <p className="member-empty">此项目还没有模板。</p> : <div className="member-list">{templates.map((item) => <div className="member-row" key={item.id}><div className="member-identity"><strong>{item.name}</strong><small>{item.category} · {item.id} · v{item.version}</small><p>{item.content}</p></div><button className="secondary-button" onClick={() => setViewingTemplate(item)} type="button">查看</button>{canEdit && <button className="secondary-button" onClick={() => { setEditingTemplate(item); setTemplateCategory(item.category as Category); setTemplateName(item.name); setTemplateContent(item.content); }} type="button">编辑</button>}{canEdit && <button className="danger-button" onClick={() => void deleteTemplate(item)} type="button">删除</button>}</div>)}</div>}
        {viewingTemplate && <div className="context-template-view-backdrop" onMouseDown={(event) => { if (event.target === event.currentTarget) setViewingTemplate(null); }} role="presentation"><section aria-labelledby="context-template-view-title" aria-modal="true" className="context-template-view" onKeyDown={(event) => { if (event.key === "Escape") setViewingTemplate(null); }} role="dialog" tabIndex={-1}><div className="context-template-view-heading"><div><h3 id="context-template-view-title">{viewingTemplate.name}</h3><p>{viewingTemplate.category} · v{viewingTemplate.version} · {viewingTemplate.id}</p></div><button aria-label="关闭模板查看" className="secondary-button" onClick={() => setViewingTemplate(null)} type="button">关闭</button></div><pre>{viewingTemplate.content}</pre></section></div>}
        {canEdit && <div className="context-template-form"><h4>{editingTemplate ? "编辑模板" : "新建模板"}</h4><p>模板创建、修改和删除均通过当前成员的项目 API Key 鉴权，并记录对应成员。</p><div className="project-form"><label><span>操作使用的 API Key</span><select aria-label="操作使用的 API Key" onChange={(event) => setSelectedKeyId(event.target.value)} value={selectedKeyId}><option value="">选择 API Key</option>{keys.map((item) => <option key={item.id} value={item.id}>{item.name} · …{item.key_last_four}</option>)}</select></label><select aria-label="模板分类" disabled={Boolean(editingTemplate)} onChange={(event) => setTemplateCategory(event.target.value as Category)} value={templateCategory}><option value="system">System</option><option value="user">User</option><option value="assistant">Assistant</option></select><input aria-label="模板名称" maxLength={200} onChange={(event) => setTemplateName(event.target.value)} placeholder="例如：support-agent" value={templateName} /></div><textarea aria-label="模板正文" maxLength={100000} onChange={(event) => setTemplateContent(event.target.value)} placeholder={'例如：你是一个助手。请用{{language}}回答以下问题：{{question}}'} rows={8} value={templateContent} /><button className="primary-button" disabled={busy || !key?.secret || !templateName.trim()} onClick={() => void saveTemplate()} type="button">{busy ? "保存中…" : editingTemplate ? "保存修改" : "创建模板"}</button></div>}
        {!canEdit && <p className="member-empty">模板当前只读；管理员和项目 viewer 可查看，项目 owner/editor 可管理。</p>}
        <div className="context-api-list"><strong>接口路径</strong><p>接入应用使用项目 API Key 调用；项目由 Key 识别，无需传 project_id。所有模板接口都在 Swagger UI（{appBasePath}/docs）的 Project Context Gateway 分类中。</p><code>GET {appBasePath}/v1/context/templates</code><code>POST {appBasePath}/v1/context/templates</code><code>GET {appBasePath}/v1/context/templates/by-id/&#123;template_id&#125;</code><code>GET {appBasePath}/v1/context/templates/&#123;category&#125;/&#123;name&#125;</code><code>PATCH {appBasePath}/v1/context/templates/&#123;template_id&#125;</code><code>DELETE {appBasePath}/v1/context/templates/&#123;template_id&#125;?expected_version=&#123;version&#125;</code></div>
      </section>}
      {section !== "templates" && <section className="project-detail-panel"><h3>{titles[section]}</h3><p>项目由当前项目路由确定；按外部用户 ID 查询。管理员使用登录态只读接口，无需查看项目 API Key。</p>
        <div className="project-form context-user-lookup"><label><span>外部用户 ID</span><input onChange={(event) => setExternalUserId(event.target.value)} placeholder="接入应用中的稳定用户标识" value={externalUserId} /></label>{section === "short" && <label><span>会话 ID</span><input onChange={(event) => setSessionId(event.target.value)} placeholder="例如：session-2026-09-30" value={sessionId} /></label>}<button className="primary-button" disabled={!canRead || busy} onClick={() => void readRecords()} type="button">{busy ? "读取中…" : "读取"}</button></div>
        {!canRead && <p className="member-empty">项目 owner/editor 可查询；管理员可以跨项目只读查询。项目 viewer 不能查询用户数据。</p>}
        {section === "profile" && <div className="context-memory-card"><h4>自定义 JSON 画像 {profileVersion ? `· v${profileVersion}` : "· 未创建"}</h4><textarea aria-label="用户画像 JSON" onChange={(event) => setProfileText(event.target.value)} readOnly={!canEdit} rows={12} value={profileText} />{canEdit && <button className="primary-button" disabled={busy || !key?.secret || !externalUserId.trim()} onClick={() => void saveProfile()} type="button">保存画像</button>}</div>}
        {section === "short" && <div className="context-memory-card"><div className="service-allocation-heading"><h4>消息历史 · 最近 {messages.length} 条</h4>{canEdit && <button className="danger-button" disabled={busy || !key?.secret || messages.length === 0} onClick={() => void clearSessionMessages()} type="button">清除会话消息</button>}</div><div className="context-session-messages">{messages.map((item) => <article key={item.id}><div><strong>{item.role}</strong><small>序号 {item.sequence} · {new Date(item.expires_at).toLocaleString()}</small></div><p>{item.content}</p></article>)}{messages.length === 0 && <p className="member-empty">没有可显示的消息。</p>}</div>{canEdit && <div className="project-form context-message-compose"><select aria-label="消息角色" onChange={(event) => setMessageRole(event.target.value as ContextSessionMessage["role"])} value={messageRole}><option value="user">user</option><option value="assistant">assistant</option><option value="system">system</option><option value="tool">tool</option></select><textarea aria-label="消息内容" onChange={(event) => setMessageDraft(event.target.value)} placeholder="追加一条消息" rows={3} value={messageDraft} /><button className="primary-button" disabled={busy || !key?.secret || !externalUserId.trim() || !sessionId.trim() || !messageDraft.trim()} onClick={() => void appendMessage()} type="button">追加消息</button></div>}</div>}
        {section === "long" && (
          <div className="context-memory-card">
            <h4>长期记忆 · {longMemories.length} 条</h4>
            {canEdit && (
              <div className="project-form">
                <input aria-label="新增长期记忆" onChange={(event) => setLongMemoryDraft(event.target.value)} placeholder="输入一条长期有效的偏好或事实" value={longMemoryDraft} />
                <button className="secondary-button" disabled={busy || !key?.secret || !externalUserId.trim() || !longMemoryDraft.trim()} onClick={() => void createLongMemory()} type="button">新增</button>
              </div>
            )}
            <div className="member-list">
              {longMemories.map((item) => (
                <div className="member-row" key={item.id}>
                  <div className="member-identity">
                    {editingMemoryId === item.id ? <textarea aria-label="编辑长期记忆" onChange={(event) => setEditingMemoryText(event.target.value)} rows={3} value={editingMemoryText} /> : <span>{item.content}</span>}
                    <small>{item.tags.join(" · ") || "无标签"} · v{item.version}</small>
                  </div>
                  {canEdit && (editingMemoryId === item.id
                    ? <button className="secondary-button" disabled={busy} onClick={() => void updateLongMemory(item)} type="button">保存</button>
                    : <button className="secondary-button" onClick={() => { setEditingMemoryId(item.id); setEditingMemoryText(item.content); }} type="button">编辑</button>)}
                  {canEdit && <button className="danger-button" disabled={busy} onClick={() => void deleteLongMemory(item)} type="button">删除</button>}
                </div>
              ))}
            </div>
          </div>
        )}
        {!canRead && <p className="member-empty">外部用户上下文只对项目 owner/editor 和管理员开放只读查询。</p>}
      </section>}
    </section>
  );
}
