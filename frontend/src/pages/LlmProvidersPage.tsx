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
  const [providerForm, setProviderForm] = useState({ name: "", base_url: "", api_key: "" });
  const [editingId, setEditingId] = useState<string | null>(null);
  const [editForm, setEditForm] = useState({ name: "", base_url: "", api_key: "" });
  const [modelDrafts, setModelDrafts] = useState<Record<string, { model_code: string; upstream_model: string }>>({});

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
      setProviderForm({ name: "", base_url: "", api_key: "" });
      setNotice("供应商连接已添加。请添加模型映射并测试连通性。");
      await loadProviders();
    } catch (reason) {
      setError(messageFor(reason));
    } finally {
      setCreating(false);
    }
  }

  function beginEdit(provider: LlmProvider) {
    setEditingId(provider.id);
    setEditForm({ name: provider.name, base_url: provider.base_url, api_key: "" });
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
        base_url: editForm.base_url.trim(),
        ...(editForm.api_key ? { api_key: editForm.api_key } : {}),
      });
      setEditingId(null);
      setEditForm({ name: "", base_url: "", api_key: "" });
      setNotice("连接配置已保存；密钥仅在服务端保存。");
      await loadProviders();
    } catch (reason) {
      setError(messageFor(reason));
    } finally {
      setBusyId(null);
    }
  }

  async function runTest(provider: LlmProvider) {
    if (!token || busyId) return;
    setBusyId(provider.id);
    setError(null);
    setNotice(null);
    try {
      const result = await apiClient.testLlmProvider(token, provider.id);
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
    if (!token || busyId || !window.confirm(`删除“${provider.name}”及其全部模型映射？此操作无法撤销。`)) return;
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

  async function addModel(provider: LlmProvider) {
    const draft = modelDrafts[provider.id];
    if (!token || !draft?.model_code.trim() || !draft.upstream_model.trim() || busyId) return;
    setBusyId(provider.id);
    setError(null);
    try {
      await apiClient.createLlmProviderModel(token, provider.id, {
        model_code: draft.model_code.trim(),
        upstream_model: draft.upstream_model.trim(),
      });
      setModelDrafts((current) => ({ ...current, [provider.id]: { model_code: "", upstream_model: "" } }));
      setNotice("模型映射已添加。相同网关模型名会自动进入同一调用池。");
      await loadProviders();
    } catch (reason) {
      setError(messageFor(reason));
    } finally {
      setBusyId(null);
    }
  }

  async function toggleModel(provider: LlmProvider, model: LlmProvider["models"][number]) {
    if (!token || busyId) return;
    setBusyId(provider.id);
    setError(null);
    try {
      await apiClient.updateLlmProviderModel(token, model.id, {
        status: model.status === "active" ? "disabled" : "active",
      });
      await loadProviders();
    } catch (reason) {
      setError(messageFor(reason));
    } finally {
      setBusyId(null);
    }
  }

  async function deleteModel(provider: LlmProvider, model: LlmProvider["models"][number]) {
    if (!token || busyId) return;
    setBusyId(provider.id);
    setError(null);
    try {
      await apiClient.deleteLlmProviderModel(token, model.id);
      await loadProviders();
    } catch (reason) {
      setError(messageFor(reason));
    } finally {
      setBusyId(null);
    }
  }

  const pools = new Map<string, number>();
  for (const provider of providers) {
    for (const model of provider.models) {
      if (model.status === "active" && provider.status === "active") {
        pools.set(model.model_code, (pools.get(model.model_code) ?? 0) + 1);
      }
    }
  }

  return (
    <section className="page-content llm-admin-page" aria-labelledby="llm-admin-title">
      <div className="intro-row">
        <div>
          <p className="page-kicker">管理员 · 服务配置</p>
          <h2 id="llm-admin-title">LLM 供应商</h2>
          <p className="page-description">配置 OpenAI 兼容上游。相同网关模型名会组成轮询池，并在上游失败时自动切换。</p>
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
          <label className="project-field"><span>Base URL</span><input maxLength={500} onChange={(event) => setProviderForm({ ...providerForm, base_url: event.target.value })} placeholder="https://api.example.com/v1" required type="url" value={providerForm.base_url} /></label>
          <label className="project-field"><span>供应商 API Key</span><input autoComplete="new-password" maxLength={4000} onChange={(event) => setProviderForm({ ...providerForm, api_key: event.target.value })} placeholder="仅提交给服务端保存" required type="password" value={providerForm.api_key} /></label>
          <button className="primary-button llm-submit" disabled={creating} type="submit">{creating ? "添加中…" : "添加连接"}</button>
        </div>
      </form>

      <section className="project-detail-panel llm-pool-panel" aria-labelledby="llm-pool-title">
        <div className="llm-section-heading"><div><h3 id="llm-pool-title">当前模型池</h3><p>池内仅统计已启用的连接与模型映射。</p></div><span className="llm-pool-count">{pools.size} 个公开模型</span></div>
        {pools.size === 0 ? <p className="llm-empty">还没有可用模型。添加连接并配置模型映射后会显示在这里。</p> : (
          <div className="llm-pool-list">{Array.from(pools.entries()).map(([code, count]) => <div className="llm-pool-chip" key={code}><code>{code}</code><span>{count} 个上游连接</span></div>)}</div>
        )}
      </section>

      <div className="llm-provider-list-heading"><div><h3>上游连接</h3><p>单个连接可提供多个网关模型映射。</p></div><span>{providers.length} 个连接</span></div>
      {loading && providers.length === 0 ? <div className="empty-state">正在加载供应商配置…</div> : null}
      {!loading && !error && providers.length === 0 ? <div className="empty-state">暂无上游连接，请使用上方表单添加。</div> : null}
      <div className="llm-provider-list">
        {providers.map((provider) => (
          <article className="project-detail-panel llm-provider-card" key={provider.id}>
            <div className="llm-provider-heading">
              <div><div className="llm-provider-title"><h3>{provider.name}</h3><span className={`service-capability-badge ${provider.status === "active" ? "service-capability-enabled" : "service-capability-disabled"}`}>{provider.status === "active" ? "已启用" : "已停用"}</span></div><p className="llm-provider-url"><code>{provider.base_url}</code></p></div>
              <div className="llm-provider-actions">
                <span className="llm-key-state">{provider.api_key_configured ? "API Key 已配置" : "缺少 API Key"}</span>
                <button className="secondary-button" disabled={busyId === provider.id} onClick={() => void runTest(provider)} type="button">{busyId === provider.id ? "处理中…" : "测试连通"}</button>
                <button className="secondary-button" disabled={busyId === provider.id} onClick={() => provider.id === editingId ? setEditingId(null) : beginEdit(provider)} type="button">{provider.id === editingId ? "取消编辑" : "编辑"}</button>
                <button className="secondary-button" disabled={busyId === provider.id} onClick={() => void toggleProvider(provider)} type="button">{provider.status === "active" ? "停用" : "启用"}</button>
                <button className="danger-button" disabled={busyId === provider.id} onClick={() => void deleteProvider(provider)} type="button">删除</button>
              </div>
            </div>

            <div className="llm-test-status" role="status"><span className={provider.last_test_success === true ? "llm-test-ok" : provider.last_test_success === false ? "llm-test-failed" : ""}>{provider.last_test_success === true ? "最近测试成功" : provider.last_test_success === false ? "最近测试失败" : "尚无测试结果"}</span><span>{testedAt(provider.last_tested_at)}</span>{provider.last_test_message && <span>{provider.last_test_message}</span>}</div>

            {provider.id === editingId && <div className="llm-provider-edit"><label className="project-field"><span>连接名称</span><input maxLength={100} onChange={(event) => setEditForm({ ...editForm, name: event.target.value })} value={editForm.name} /></label><label className="project-field"><span>Base URL</span><input maxLength={500} onChange={(event) => setEditForm({ ...editForm, base_url: event.target.value })} type="url" value={editForm.base_url} /></label><label className="project-field"><span>轮换 API Key <small>留空保留当前密钥</small></span><input autoComplete="new-password" onChange={(event) => setEditForm({ ...editForm, api_key: event.target.value })} placeholder="输入新密钥以轮换" type="password" value={editForm.api_key} /></label><button className="primary-button" disabled={busyId === provider.id} onClick={() => void saveProvider(provider)} type="button">保存连接</button></div>}

            <div className="llm-model-section"><div className="llm-model-heading"><h4>模型映射</h4><span>{provider.models.length} 个</span></div>
              {provider.models.length === 0 ? <p className="llm-empty-inline">此连接还没有模型映射。</p> : <div className="llm-model-list">{provider.models.map((model) => <div className="llm-model-row" key={model.id}><div><code>{model.model_code}</code><span aria-hidden="true">→</span><code>{model.upstream_model}</code><span className={`service-capability-badge ${model.status === "active" ? "service-capability-enabled" : "service-capability-disabled"}`}>{model.status === "active" ? "启用" : "停用"}</span></div><div><button className="secondary-button" disabled={busyId === provider.id} onClick={() => void toggleModel(provider, model)} type="button">{model.status === "active" ? "停用" : "启用"}</button><button className="danger-button" disabled={busyId === provider.id} onClick={() => void deleteModel(provider, model)} type="button">移除</button></div></div>)}</div>}
              <div className="llm-add-model"><label className="project-field"><span>网关公开模型名</span><input onChange={(event) => setModelDrafts((current) => ({ ...current, [provider.id]: { model_code: event.target.value, upstream_model: current[provider.id]?.upstream_model ?? "" } }))} placeholder="例如：chat-standard" value={modelDrafts[provider.id]?.model_code ?? ""} /></label><label className="project-field"><span>供应商模型名</span><input onChange={(event) => setModelDrafts((current) => ({ ...current, [provider.id]: { model_code: current[provider.id]?.model_code ?? "", upstream_model: event.target.value } }))} placeholder="例如：gpt-4o-mini" value={modelDrafts[provider.id]?.upstream_model ?? ""} /></label><button className="secondary-button" disabled={busyId === provider.id || !modelDrafts[provider.id]?.model_code.trim() || !modelDrafts[provider.id]?.upstream_model.trim()} onClick={() => void addModel(provider)} type="button">添加模型</button></div>
            </div>
          </article>
        ))}
      </div>
    </section>
  );
}
