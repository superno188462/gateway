import { useCallback, useEffect, useMemo, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { ApiClientError, apiClient, type ApiKey, type Project, type ProjectMember } from "../api/client";
import { useAuth } from "../auth/useAuth";

type ExpiryOption = "never" | "30" | "90" | "365";

function errorMessage(reason: unknown): string {
  if (reason instanceof ApiClientError) {
    if (reason.status === 503) return "API Key 功能尚未启用，请联系管理员配置 API_KEY_SECRET_KEY。";
    if (reason.status === 403) return "只有项目 owner 和 editor 可以查看 API Key 元数据。";
    return reason.message;
  }
  return "请求失败，请检查网络后重试。";
}

function formatDate(value: string): string {
  return new Date(value).toLocaleString();
}

export function ProjectApiKeysPage() {
  const { projectId } = useParams<{ projectId: string }>();
  const { token, user } = useAuth();
  const [project, setProject] = useState<Project | null>(null);
  const [members, setMembers] = useState<ProjectMember[]>([]);
  const [keys, setKeys] = useState<ApiKey[]>([]);
  const [isLoading, setIsLoading] = useState(true);
  const [isCreating, setIsCreating] = useState(false);
  const [revokingId, setRevokingId] = useState<string | null>(null);
  const [name, setName] = useState("");
  const [expiry, setExpiry] = useState<ExpiryOption>("never");
  const [secret, setSecret] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);
  const [copiedKeyId, setCopiedKeyId] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [copyError, setCopyError] = useState(false);

  const role = useMemo(() => {
    if (!user || !project) return null;
    if (project.owner_id === user.id) return "owner";
    return members.find((member) => member.user_id === user.id)?.role ?? null;
  }, [members, project, user]);
  const canView = role === "owner" || role === "editor";
  const canManage = role === "owner";

  const load = useCallback(async () => {
    if (!token || !projectId) return;
    setIsLoading(true);
    setError(null);
    try {
      const [loadedProject, loadedMembers] = await Promise.all([
        apiClient.getProject(token, projectId),
        apiClient.getProjectMembers(token, projectId),
      ]);
      setProject(loadedProject);
      setMembers(loadedMembers);
      const loadedRole = loadedProject.owner_id === user?.id
        ? "owner"
        : loadedMembers.find((member) => member.user_id === user?.id)?.role;
      if (loadedRole === "owner" || loadedRole === "editor") {
        setKeys(await apiClient.getProjectApiKeys(token, projectId));
      } else {
        setKeys([]);
      }
    } catch (reason) {
      setError(errorMessage(reason));
    } finally {
      setIsLoading(false);
    }
  }, [projectId, token, user?.id]);

  useEffect(() => {
    void load();
  }, [load]);

  async function createKey() {
    if (!token || !projectId || !name.trim() || isCreating) return;
    setIsCreating(true);
    setError(null);
    setCopyError(false);
    try {
      const expiresAt = expiry === "never"
        ? null
        : new Date(Date.now() + Number(expiry) * 24 * 60 * 60 * 1000).toISOString();
      const created = await apiClient.createProjectApiKey(token, projectId, {
        name: name.trim(),
        expires_at: expiresAt,
      });
      setKeys((current) => [created, ...current]);
      setSecret(created.secret);
      setCopied(false);
      setName("");
      setExpiry("never");
    } catch (reason) {
      setError(errorMessage(reason));
    } finally {
      setIsCreating(false);
    }
  }

  async function revokeKey(key: ApiKey) {
    if (!token || !projectId || revokingId) return;
    if (!window.confirm(`确定撤销“${key.name}”吗？使用该 Key 的客户端将立即无法调用此项目。`)) return;
    setRevokingId(key.id);
    setError(null);
    try {
      await apiClient.revokeProjectApiKey(token, projectId, key.id);
      setKeys((current) => current.map((item) => item.id === key.id
        ? { ...item, status: "revoked", revoked_at: new Date().toISOString() }
        : item));
    } catch (reason) {
      setError(errorMessage(reason));
    } finally {
      setRevokingId(null);
    }
  }

  async function copySecret() {
    if (!secret) return;
    try {
      await navigator.clipboard.writeText(secret);
      setCopied(true);
      setCopyError(false);
    } catch {
      setCopyError(true);
    }
  }

  async function copyKey(key: ApiKey) {
    if (!key.secret) return;
    try {
      await navigator.clipboard.writeText(key.secret);
      setCopiedKeyId(key.id);
    } catch {
      setError("复制失败，请手动选择并复制完整 API Key。");
    }
  }

  if (isLoading) {
    return <section className="page-content"><div className="empty-state">正在加载 API Key…</div></section>;
  }

  return (
    <section className="page-content" aria-labelledby="api-keys-title">
      <Link className="back-link" to={projectId ? `/projects/${projectId}` : "/projects"}>
        ← 返回项目
      </Link>
      <div className="intro-row api-keys-intro">
        <div>
          <p className="page-kicker">项目凭证</p>
          <h2 id="api-keys-title">API Key</h2>
          <p className="page-description">{project?.name ?? "项目"} · 每把 Key 都授权整个项目。</p>
        </div>
        <button className="secondary-button" onClick={() => void load()} type="button">刷新</button>
      </div>

      {error && <p className="form-error api-key-error" role="alert">{error}</p>}

      {!project ? (
        <div className="empty-state">{error || "项目不存在或你没有访问权限。"}</div>
      ) : !canView ? (
        <div className="empty-state">此页面仅对该项目的 owner 和 editor 开放。</div>
      ) : (
        <>
          <section className="project-detail-panel api-key-panel" aria-labelledby="api-key-list-title">
            <div className="api-key-heading">
              <div>
                <h3 id="api-key-list-title">项目密钥</h3>
                <p>owner 和 editor 都可随时查看完整密钥；请仅将其提供给可信任的调用方。</p>
              </div>
              <span className="api-key-role-badge">{role}</span>
            </div>

            {canManage && (
              <form className="api-key-create-form" onSubmit={(event) => { event.preventDefault(); void createKey(); }}>
                <label className="api-key-field">
                  <span>Key 名称</span>
                  <input
                    maxLength={100}
                    onChange={(event) => setName(event.target.value)}
                    placeholder="例如：生产环境 / Agent 服务"
                    required
                    value={name}
                  />
                </label>
                <label className="api-key-field">
                  <span>有效期</span>
                  <select onChange={(event) => setExpiry(event.target.value as ExpiryOption)} value={expiry}>
                    <option value="never">永不过期</option>
                    <option value="30">30 天</option>
                    <option value="90">90 天</option>
                    <option value="365">365 天</option>
                  </select>
                </label>
                <button className="primary-button" disabled={!name.trim() || isCreating} type="submit">
                  {isCreating ? "创建中…" : "创建 API Key"}
                </button>
              </form>
            )}

            {keys.length === 0 ? (
              <div className="empty-state api-key-empty">暂无 API Key{canManage ? "，可以创建第一把 Key。" : "。"}</div>
            ) : (
              <div className="api-key-list">
                {keys.map((key) => (
                  <article className="api-key-row" key={key.id}>
                    <div className="api-key-main">
                      <div className="api-key-name-row">
                        <strong>{key.name}</strong>
                        <span className={`api-key-status api-key-status-${key.status}`}>
                          {key.status === "active" ? "有效" : key.status === "expired" ? "已过期" : "已撤销"}
                        </span>
                      </div>
                      <code className="api-key-full-secret">{key.secret ?? `${key.key_prefix}…${key.key_last_four}（旧 Key 无法恢复）`}</code>
                      <div className="api-key-meta">
                        <span>创建于 {formatDate(key.created_at)}</span>
                        <span>有效期至 {key.expires_at ? formatDate(key.expires_at) : "永不过期"}</span>
                        <span>最近使用 {key.last_used_at ? formatDate(key.last_used_at) : "从未使用"}</span>
                      </div>
                    </div>
                    <div className="api-key-row-actions">
                      {key.secret && (
                        <button className="secondary-button" onClick={() => void copyKey(key)} type="button">
                          {copiedKeyId === key.id ? "已复制" : "复制 Key"}
                        </button>
                      )}
                      {canManage && key.status === "active" && (
                        <button
                          className="danger-button"
                          disabled={revokingId === key.id}
                          onClick={() => void revokeKey(key)}
                          type="button"
                        >
                          {revokingId === key.id ? "撤销中…" : "撤销"}
                        </button>
                      )}
                    </div>
                  </article>
                ))}
              </div>
            )}
          </section>
        </>
      )}

      {secret && (
        <div className="api-key-modal-backdrop" role="presentation">
          <section
            aria-labelledby="api-key-secret-title"
            aria-modal="true"
            className="api-key-modal"
            role="dialog"
          >
            <div className="api-key-modal-heading">
              <span className="api-key-modal-icon" aria-hidden="true">!</span>
              <div>
                <h3 id="api-key-secret-title">API Key 已创建</h3>
                <p>owner 和 editor 可随时在项目 Key 列表中查看完整密钥。</p>
              </div>
            </div>
            <code className="api-key-secret-value">{secret}</code>
            {copyError && <p className="form-error" role="alert">复制失败，请手动复制上方密钥。</p>}
            <div className="api-key-modal-actions">
              <button className="secondary-button" onClick={() => void copySecret()} type="button">
                {copied ? "已复制" : "复制密钥"}
              </button>
              <button className="primary-button" onClick={() => setSecret(null)} type="button">
                我已保存，关闭
              </button>
            </div>
          </section>
        </div>
      )}
    </section>
  );
}
