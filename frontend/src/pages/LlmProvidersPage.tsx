import { useCallback, useEffect, useState } from "react";
import type { FormEvent } from "react";
import { ApiClientError, apiClient, type LlmProvider } from "../api/client";
import { useAuth } from "../auth/useAuth";

function messageFor(reason: unknown): string {
  if (reason instanceof ApiClientError) return reason.message;
  return reason instanceof Error ? reason.message : "操作失败，请稍后重试。";
}

function testedAt(value: string | null): string {
  if (!value) return "尚未测试";
  return new Date(value).toLocaleString("zh-CN");
}

export function LlmProvidersPage() {
  const { token } = useAuth();
  const [providers, setProviders] = useState<LlmProvider[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [busyId, setBusyId] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);
  const [providerForm, setProviderForm] = useState({ name: "", route_prefix: "", base_url: "", api_key: "" });
  const [editingId, setEditingId] = useState<string | null>(null);
  const [editForm, setEditForm] = useState({ name: "", route_prefix: "", base_url: "", api_key: "", priority: "100" });
  const [testModels, setTestModels] = useState<Record<string, string>>({});

  const loadProviders = useCallback(async () => {
    if (!token) return;
    setLoading(true);
    setError(null);
    try {
      setProviders(await apiClient.getLlmProviders(token));
    } catch (reason) {
      setError(messageFor(reason));
    } finally {
      setLoading(false);
    }
  }, [token]);

  useEffect(() => { void loadProviders(); }, [loadProviders]);

  async function createProvider(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!token || creating) return;
    setCreating(true);
    setError(null);
    setNotice(null);
    try {
      await apiClient.createLlmProvider(token, providerForm);
      setProviderForm({ name: "", route_prefix: "", base_url: "", api_key: "" });
      setNotice("供应商连接已添加。模型名会原样转发，上游按优先级调用。");
      await loadProviders();
    } catch (reason) {
      setError(messageFor(reason));
    } finally {
      setCreating(false);
    }
  }

  function beginEdit(provider: LlmProvider) {
    setEditingId(provider.id);
    setEditForm({ name: provider.name, route_prefix: provider.route_prefix ?? "", base_url: provider.base_url, api_key: "", priority: String(provider.priority) });
    setError(null);
    setNotice(null);
  }

  async function saveProvider(provider: LlmProvider) {
    if (!token || busyId) return;
    setBusyId(provider.id);
    setError(null);
    try {
      await apiClient.updateLlmProvider(token, provider.id, {
        name: editForm.name.trim(),
        route_prefix: editForm.route_prefix.trim(),
        base_url: editForm.base_url.trim(),
        priority: Number(editForm.priority),
        ...(editForm.api_key ? { api_key: editForm.api_key } : {}),
      });
      setEditingId(null);
      setEditForm({ name: "", route_prefix: "", base_url: "", api_key: "", priority: "100" });
      setNotice("连接配置已保存；密钥仅在服务端保存。");
      await loadProviders();
    } catch (reason) {
      setError(messageFor(reason));
    } finally {
      setBusyId(null);
    }
  }

  async function runTest(provider: LlmProvider) {
    const model = testModels[provider.id]?.trim();
    if (!token || busyId || !model) return;
    setBusyId(provider.id);
    setError(null);
    setNotice(null);
    try {
      const result = await apiClient.testLlmProvider(token, provider.id, model);
      if (result.success) {
        setNotice(`${provider.name}：${result.message}（${testedAt(result.tested_at)}）`);
      } else {
        setError(`${provider.name}：${result.message}`);
      }
      await loadProviders();
    } catch (reason) {
      setError(messageFor(reason));
      await loadProviders();
    } finally {
      setBusyId(null);
    }
  }

  async function toggleProvider(provider: LlmProvider) {
    if (!token || busyId) return;
    setBusyId(provider.id);
    setError(null);
    try {
      await apiClient.updateLlmProvider(token, provider.id, {
        status: provider.status === "active" ? "disabled" : "active",
      });
      await loadProviders();
    } catch (reason) {
      setError(messageFor(reason));
    } finally {
      setBusyId(null);
    }
  }

  async function deleteProvider(provider: LlmProvider) {
    if (!token || busyId || !window.confirm(`删除“${provider.name}”连接？此操作无法撤销。`)) return;
    setBusyId(provider.id);
    setError(null);
    try {
      await apiClient.deleteLlmProvider(token, provider.id);
      setNotice(`已删除连接 ${provider.name}。`);
      await loadProviders();
    } catch (reason) {
      setError(messageFor(reason));
    } finally {
      setBusyId(null);
    }
  }

  return (
    <section className="page-content llm-admin-page" aria-labelledby="llm-admin-title">
      <div className="intro-row">
        <div>
          <p className="page-kicker">管理员 · 服务配置</p>
          <h2 id="llm-admin-title">LLM 供应商</h2>
          <p className="page-description">配置 OpenAI 兼容上游。模型名默认原样转发，也可用已配置前缀指定供应商组；组内按优先级切换。</p>
        </div>
        <button className="secondary-button" disabled={loading} onClick={() => void loadProviders()} type="button">
          {loading ? "刷新中…" : "刷新"}
        </button>
      </div>

      {error && <div className="service-page-error" role="alert"><p>{error}</p><button className="secondary-button" onClick={() => void loadProviders()} type="button">重试</button></div>}
      {notice && <p className="form-success llm-feedback" role="status">{notice}</p>}

      <form className="project-detail-panel llm-provider-create" onSubmit={(event) => void createProvider(event)}>
        <div>
          <h3>添加上游连接</h3>
          <p>API Key 加密保存在服务端；页面不会再次读取或显示密钥。</p>
        </div>
        <div className="llm-provider-form-grid">
          <label className="project-field"><span>连接名称</span><input maxLength={100} onChange={(event) => setProviderForm({ ...providerForm, name: event.target.value })} placeholder="例如：主供应商" required value={providerForm.name} /></label>
          <label className="project-field"><span>模型路由前缀 <small>可选；同前缀组成一组</small></span><input maxLength={64} onChange={(event) => setProviderForm({ ...providerForm, route_prefix: event.target.value })} placeholder="例如：volc；留空加入默认池" value={providerForm.route_prefix} /></label>
          <label className="project-field"><span>Base URL</span><input maxLength={500} onChange={(event) => setProviderForm({ ...providerForm, base_url: event.target.value })} placeholder="https://api.example.com/v1" required type="url" value={providerForm.base_url} /></label>
          <label className="project-field"><span>供应商 API Key</span><input autoComplete="new-password" maxLength={4000} onChange={(event) => setProviderForm({ ...providerForm, api_key: event.target.value })} placeholder="仅提交给服务端保存" required type="password" value={providerForm.api_key} /></label>
          <button className="primary-button llm-submit" disabled={creating} type="submit">{creating ? "添加中…" : "添加连接"}</button>
        </div>
      </form>

      <div className="llm-provider-list-heading"><div><h3>上游连接</h3><p>不带前缀时所有已启用连接组成默认池；使用“前缀/模型名”时只调用同前缀连接。优先级数字越小越先尝试。</p></div><span>{providers.length} 个连接</span></div>
      {loading && providers.length === 0 ? <div className="empty-state">正在加载供应商配置…</div> : null}
      {!loading && !error && providers.length === 0 ? <div className="empty-state">暂无上游连接，请使用上方表单添加。</div> : null}
      <div className="llm-provider-list">
        {providers.map((provider) => (
          <article className="project-detail-panel llm-provider-card" key={provider.id}>
            <div className="llm-provider-heading">
              <div><div className="llm-provider-title"><h3>{provider.name}</h3><span className={`service-capability-badge ${provider.status === "active" ? "service-capability-enabled" : "service-capability-disabled"}`}>{provider.status === "active" ? "已启用" : "已停用"}</span><span className="llm-key-state">{provider.route_prefix ? `前缀 ${provider.route_prefix}` : "默认池"} · 优先级 {provider.priority}</span></div><p className="llm-provider-url"><code>{provider.base_url}</code></p></div>
              <div className="llm-provider-actions">
                <span className="llm-key-state">{provider.api_key_configured ? "API Key 已配置" : "缺少 API Key"}</span>
                <button className="secondary-button" disabled={busyId === provider.id || !testModels[provider.id]?.trim()} onClick={() => void runTest(provider)} type="button">{busyId === provider.id ? "处理中…" : "测试连通"}</button>
                <button className="secondary-button" disabled={busyId === provider.id} onClick={() => provider.id === editingId ? setEditingId(null) : beginEdit(provider)} type="button">{provider.id === editingId ? "取消编辑" : "编辑"}</button>
                <button className="secondary-button" disabled={busyId === provider.id} onClick={() => void toggleProvider(provider)} type="button">{provider.status === "active" ? "停用" : "启用"}</button>
                <button className="danger-button" disabled={busyId === provider.id} onClick={() => void deleteProvider(provider)} type="button">删除</button>
              </div>
            </div>

            <div className="llm-test-status" role="status"><label className="project-field"><span>测试模型名 <small>输入供应商原始模型名，不加路由前缀</small></span><input maxLength={200} onChange={(event) => setTestModels((current) => ({ ...current, [provider.id]: event.target.value }))} placeholder="例如：doubao-seed-2-0-lite" value={testModels[provider.id] ?? ""} /></label><span className={provider.last_test_success === true ? "llm-test-ok" : provider.last_test_success === false ? "llm-test-failed" : ""}>{provider.last_test_success === true ? "最近测试成功" : provider.last_test_success === false ? "最近测试失败" : "尚无测试结果"}</span><span>{testedAt(provider.last_tested_at)}</span>{provider.last_test_message && <span>{provider.last_test_message}</span>}</div>

            {provider.id === editingId && <div className="llm-provider-edit"><label className="project-field"><span>连接名称</span><input maxLength={100} onChange={(event) => setEditForm({ ...editForm, name: event.target.value })} value={editForm.name} /></label><label className="project-field"><span>模型路由前缀 <small>清空表示移除</small></span><input maxLength={64} onChange={(event) => setEditForm({ ...editForm, route_prefix: event.target.value })} value={editForm.route_prefix} /></label><label className="project-field"><span>Base URL</span><input maxLength={500} onChange={(event) => setEditForm({ ...editForm, base_url: event.target.value })} type="url" value={editForm.base_url} /></label><label className="project-field"><span>轮换 API Key <small>留空保留当前密钥</small></span><input autoComplete="new-password" onChange={(event) => setEditForm({ ...editForm, api_key: event.target.value })} placeholder="输入新密钥以轮换" type="password" value={editForm.api_key} /></label><label className="project-field"><span>优先级 <small>数值越小越优先</small></span><input max="1000000" min="0" onChange={(event) => setEditForm({ ...editForm, priority: event.target.value })} type="number" value={editForm.priority} /></label><button className="primary-button" disabled={busyId === provider.id} onClick={() => void saveProvider(provider)} type="button">保存连接</button></div>}
          </article>
        ))}
      </div>
    </section>
  );
}
