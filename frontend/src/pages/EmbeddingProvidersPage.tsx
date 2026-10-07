import { useCallback, useEffect, useState } from "react";
import type { FormEvent } from "react";
import { apiClient, type LlmProvider } from "../api/client";
import { useAuth } from "../auth/useAuth";

export function EmbeddingProvidersPage() {
  const { token } = useAuth();
  const [providers, setProviders] = useState<LlmProvider[]>([]);
  const [form, setForm] = useState({ supplier_name: "", name: "", route_prefix: "", base_url: "" });
  const [keys, setKeys] = useState("");
  const [testModels, setTestModels] = useState<Record<string, string>>({});
  const [editingId, setEditingId] = useState<string | null>(null);
  const [editForm, setEditForm] = useState({ supplier_name: "", name: "", route_prefix: "", base_url: "", api_key: "", priority: "100" });
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    if (!token) return;
    setError("");
    try { setProviders(await apiClient.getEmbeddingProviders(token)); }
    catch (reason) { setError(reason instanceof Error ? reason.message : "读取 Embedding 配置失败"); }
  }, [token]);
  useEffect(() => { void load(); }, [load]);

  async function create(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!token || busy) return;
    const rows = keys.split(/\r?\n/).map((key) => key.trim()).filter(Boolean);
    if (!rows.length) { setError("请至少输入一个 API Key。"); return; }
    setBusy(true); setError(""); setNotice("");
    const errors: string[] = [];
    for (const [index, api_key] of rows.entries()) {
      try { await apiClient.createEmbeddingProvider(token, { ...form, route_prefix: form.route_prefix.trim() || undefined, api_key }); }
      catch (reason) { errors.push(`第 ${index + 1} 个 Key：${reason instanceof Error ? reason.message : "保存失败"}`); }
    }
    setKeys("");
    setNotice(`已保存 ${rows.length - errors.length}/${rows.length} 个连接。`);
    setError(errors.join("；"));
    await load(); setBusy(false);
  }

  async function update(provider: LlmProvider, payload: Parameters<typeof apiClient.updateEmbeddingProvider>[2]): Promise<boolean> {
    if (!token) return false;
    setBusy(true); setError("");
    try { await apiClient.updateEmbeddingProvider(token, provider.id, payload); await load(); return true; }
    catch (reason) { setError(reason instanceof Error ? reason.message : "更新失败"); return false; }
    finally { setBusy(false); }
  }

  async function remove(provider: LlmProvider) {
    if (!token || !window.confirm("删除此 Embedding 上游连接？")) return;
    setBusy(true); setError("");
    try { await apiClient.deleteEmbeddingProvider(token, provider.id); await load(); }
    catch (reason) { setError(reason instanceof Error ? reason.message : "删除失败"); }
    finally { setBusy(false); }
  }

  async function test(provider: LlmProvider) {
    if (!token) return;
    setBusy(true); setError("");
    try {
      const result = await apiClient.testEmbeddingProvider(token, provider.id, testModels[provider.id] ?? "");
      setNotice(result.message); await load();
    } catch (reason) { setError(reason instanceof Error ? reason.message : "连接测试失败"); await load(); }
    finally { setBusy(false); }
  }

  function beginEdit(provider: LlmProvider) {
    setEditingId(provider.id);
    setEditForm({ supplier_name: provider.supplier_name, name: provider.name, route_prefix: provider.route_prefix ?? "", base_url: provider.base_url, api_key: "", priority: String(provider.priority) });
  }

  async function saveEdit(provider: LlmProvider) {
    if (await update(provider, { supplier_name: editForm.supplier_name, name: editForm.name, route_prefix: editForm.route_prefix || null, base_url: editForm.base_url, api_key: editForm.api_key || undefined, priority: Number(editForm.priority) })) {
      setEditingId(null); setEditForm({ ...editForm, api_key: "" });
    }
  }

  return <section className="page-content llm-admin-page" aria-labelledby="embedding-admin-title">
    <div className="intro-row"><div><p className="page-kicker">管理员 · 服务配置</p><h2 id="embedding-admin-title">Embedding API</h2><p className="page-description">配置 OpenAI 兼容的文本向量上游。模型名前缀按 LLM 方式路由；图像与视频模型暂未接入。</p></div><button className="secondary-button" disabled={busy} onClick={() => void load()} type="button">刷新</button></div>
    {error && <p className="form-error" role="alert">{error}</p>}{notice && <p className="form-success" role="status">{notice}</p>}
    <form className="project-detail-panel llm-provider-create" onSubmit={(event) => void create(event)}>
      <h3>添加 Embedding API 组</h3><p className="page-description">一组共用供应商、连接名、模型路由前缀和 Base URL。API Key 每行一个，逐个保存。</p>
      <div className="llm-provider-form-grid">
        <label className="project-field"><span>供应商名称</span><input required value={form.supplier_name} onChange={(event) => setForm({ ...form, supplier_name: event.target.value })} placeholder="例如：OpenAI" /></label>
        <label className="project-field"><span>连接名称</span><input required value={form.name} onChange={(event) => setForm({ ...form, name: event.target.value })} placeholder="例如：主账号" /></label>
        <label className="project-field"><span>模型路由前缀</span><input value={form.route_prefix} onChange={(event) => setForm({ ...form, route_prefix: event.target.value })} placeholder="例如：openai" /></label>
        <label className="project-field"><span>Base URL</span><input required type="url" value={form.base_url} onChange={(event) => setForm({ ...form, base_url: event.target.value })} placeholder="https://api.openai.com/v1" /></label>
      </div>
      <label className="project-field llm-import-field"><span>API Key <small>每行一个</small></span><textarea rows={4} autoComplete="off" spellCheck={false} value={keys} onChange={(event) => setKeys(event.target.value)} placeholder={"API_KEY_1\nAPI_KEY_2"} /></label>
      <div className="llm-import-actions"><span>密钥加密保存在服务端；前缀为调用方模型名的一部分，例如 openai/text-embedding-3-small。</span><button className="primary-button" disabled={busy} type="submit">{busy ? "处理中…" : "添加 Embedding API"}</button></div>
    </form>
    <div className="llm-provider-list-heading"><div><h3>上游连接</h3><p>每条 API Key 独立启停、测试和设置优先级。</p></div><span>{providers.length} 个连接</span></div>
    {providers.length === 0 ? <div className="empty-state">暂无连接，请先添加 OpenAI 兼容的文本 Embedding API。</div> : <div className="llm-provider-list">{providers.map((provider) => <article className="project-detail-panel llm-provider-card" key={provider.id}>
      <div className="llm-provider-heading"><div><div className="llm-provider-title"><h3>{provider.supplier_name} · {provider.name}</h3><span className="llm-key-state">{provider.route_prefix || "默认路由"} · 优先级 {provider.priority}</span></div><p className="llm-provider-url"><code>{provider.base_url}</code></p><p>API Key：{provider.api_key_configured ? "已加密保存" : "未配置"} · {provider.last_test_success === true ? "最近测试成功" : provider.last_test_success === false ? "最近测试失败" : "尚未测试"}{provider.last_test_message ? ` · ${provider.last_test_message}` : ""}</p></div>
        <div className="llm-provider-actions"><button className="secondary-button" disabled={busy} onClick={() => setEditingId(editingId === provider.id ? null : (beginEdit(provider), provider.id))} type="button">{editingId === provider.id ? "取消编辑" : "编辑"}</button><button className="secondary-button" disabled={busy} onClick={() => void update(provider, { status: provider.status === "active" ? "disabled" : "active" })} type="button">{provider.status === "active" ? "停用" : "启用"}</button><button className="danger-button" disabled={busy} onClick={() => void remove(provider)} type="button">删除</button></div></div>
      <p className="llm-provider-url">API Key：<code>{provider.api_key ?? "密钥不可解密，请检查服务端加密配置"}</code></p>
      {editingId === provider.id && <div className="llm-provider-edit"><label className="project-field"><span>供应商名称</span><input value={editForm.supplier_name} onChange={(event) => setEditForm({ ...editForm, supplier_name: event.target.value })} /></label><label className="project-field"><span>连接名称</span><input value={editForm.name} onChange={(event) => setEditForm({ ...editForm, name: event.target.value })} /></label><label className="project-field"><span>路由前缀</span><input value={editForm.route_prefix} onChange={(event) => setEditForm({ ...editForm, route_prefix: event.target.value })} /></label><label className="project-field"><span>Base URL</span><input type="url" value={editForm.base_url} onChange={(event) => setEditForm({ ...editForm, base_url: event.target.value })} /></label><label className="project-field"><span>轮换 API Key <small>留空保留</small></span><input autoComplete="new-password" type="password" value={editForm.api_key} onChange={(event) => setEditForm({ ...editForm, api_key: event.target.value })} /></label><label className="project-field"><span>优先级</span><input type="number" min="0" max="1000000" value={editForm.priority} onChange={(event) => setEditForm({ ...editForm, priority: event.target.value })} /></label><button className="primary-button" disabled={busy} onClick={() => void saveEdit(provider)} type="button">保存连接</button></div>}
      <div className="llm-test-status"><label className="project-field"><span>测试模型</span><input value={testModels[provider.id] ?? ""} onChange={(event) => setTestModels({ ...testModels, [provider.id]: event.target.value })} placeholder="例如 text-embedding-3-small" /></label><button className="secondary-button" disabled={busy || !testModels[provider.id]?.trim()} onClick={() => void test(provider)} type="button">测试连接</button><label className="project-field"><span>优先级（小者优先）</span><input type="number" min="0" max="1000000" defaultValue={provider.priority} onBlur={(event) => { const value = Number(event.target.value); if (value !== provider.priority) void update(provider, { priority: value }); }} /></label></div>
    </article>)}</div>}
  </section>;
}
