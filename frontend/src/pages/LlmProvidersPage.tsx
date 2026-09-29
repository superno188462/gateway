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
  const [copiedKeyId, setCopiedKeyId] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);
  const [apiKeyText, setApiKeyText] = useState("");
  const [groupForm, setGroupForm] = useState({ supplier_name: "", name: "", route_prefix: "", base_url: "" });
  const [importResults, setImportResults] = useState<Array<{ line: number; success: boolean; message: string }>>([]);
  const [editingId, setEditingId] = useState<string | null>(null);
  const [editForm, setEditForm] = useState({ supplier_name: "", name: "", route_prefix: "", base_url: "", api_key: "", priority: "100" });
  const [testModels, setTestModels] = useState<Record<string, string>>({});

  const apiNumberById = new Map<string, number>();
  const apiCounts = new Map<string, number>();
  for (const provider of providers) {
    const groupKey = JSON.stringify([
      provider.supplier_name,
      provider.name,
      provider.route_prefix,
      provider.base_url,
    ]);
    const nextNumber = (apiCounts.get(groupKey) ?? 0) + 1;
    apiCounts.set(groupKey, nextNumber);
    apiNumberById.set(provider.id, nextNumber);
  }

  const providerGroupsByKey = new Map<string, {
    key: string;
    supplier_name: string;
    name: string;
    route_prefix: string | null;
    base_url: string;
    providers: LlmProvider[];
  }>();
  for (const provider of providers) {
    const key = JSON.stringify([
      provider.supplier_name,
      provider.name,
      provider.route_prefix,
      provider.base_url,
    ]);
    const group = providerGroupsByKey.get(key) ?? {
      key,
      supplier_name: provider.supplier_name,
      name: provider.name,
      route_prefix: provider.route_prefix,
      base_url: provider.base_url,
      providers: [],
    };
    group.providers.push(provider);
    providerGroupsByKey.set(key, group);
  }
  const providerGroups = Array.from(providerGroupsByKey.values());

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

  async function importProviders(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!token || creating) return;
    setCreating(true);
    setError(null);
    setNotice(null);
    const keys = apiKeyText.split(/\r?\n/).map((key, index) => ({ line: index + 1, key: key.trim() })).filter((row) => row.key);
    if (!groupForm.supplier_name.trim() || !groupForm.name.trim() || !groupForm.base_url.trim()) {
      setError("请填写供应商名称、连接名称和 Base URL。");
      setCreating(false);
      return;
    }
    if (keys.length === 0) {
      setError("请至少填写一个 API Key，每行一个。");
      setCreating(false);
      return;
    }
    const results: Array<{ line: number; success: boolean; message: string }> = [];
    const seenKeys = new Set<string>();
    const normalizedBaseUrl = groupForm.base_url.trim().replace(/\/+$/, "");
    for (const row of keys) {
      const alreadyStored = providers.some((provider) =>
        provider.base_url.replace(/\/+$/, "") === normalizedBaseUrl
        && provider.api_key === row.key,
      );
      if (alreadyStored) {
        results.push({ line: row.line, success: false, message: `API Key ${row.line}：仓库中已存在相同地址和密钥的 API，未重复新增` });
        continue;
      }
      if (seenKeys.has(row.key)) {
        results.push({ line: row.line, success: false, message: `API Key ${row.line}：重复内容，未保存` });
        continue;
      }
      seenKeys.add(row.key);
      try {
        await apiClient.createLlmProvider(token, {
          ...groupForm,
          supplier_name: groupForm.supplier_name.trim(),
          name: groupForm.name.trim(),
          route_prefix: groupForm.route_prefix.trim() || undefined,
          base_url: groupForm.base_url.trim(),
          api_key: row.key,
        });
        results.push({ line: row.line, success: true, message: `API Key ${row.line}：已添加` });
      } catch (reason) {
        results.push({ line: row.line, success: false, message: `API Key ${row.line}：${messageFor(reason)}` });
      }
    }
    setImportResults(results);
    setApiKeyText("");
    const successCount = results.filter((result) => result.success).length;
    setNotice(`API 添加完成：成功 ${successCount} 个，失败 ${results.length - successCount} 个。每个 API Key 可在下方单独测试。`);
    await loadProviders();
    setCreating(false);
  }

  function beginEdit(provider: LlmProvider) {
    setEditingId(provider.id);
    setEditForm({ supplier_name: provider.supplier_name, name: provider.name, route_prefix: provider.route_prefix ?? "", base_url: provider.base_url, api_key: "", priority: String(provider.priority) });
    setError(null);
    setNotice(null);
  }

  async function saveProvider(provider: LlmProvider) {
    if (!token || busyId) return;
    setBusyId(provider.id);
    setError(null);
    try {
      await apiClient.updateLlmProvider(token, provider.id, {
        supplier_name: editForm.supplier_name.trim(),
        name: editForm.name.trim(),
        route_prefix: editForm.route_prefix.trim(),
        base_url: editForm.base_url.trim(),
        priority: Number(editForm.priority),
        ...(editForm.api_key ? { api_key: editForm.api_key } : {}),
      });
      setEditingId(null);
      setEditForm({ supplier_name: "", name: "", route_prefix: "", base_url: "", api_key: "", priority: "100" });
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

  async function copyApiKey(provider: LlmProvider) {
    if (!provider.api_key) return;
    try {
      await navigator.clipboard.writeText(provider.api_key);
      setCopiedKeyId(provider.id);
      window.setTimeout(() => setCopiedKeyId(null), 1800);
    } catch {
      setError("复制失败，请检查浏览器剪贴板权限。");
    }
  }

  return (
    <section className="page-content llm-admin-page" aria-labelledby="llm-admin-title">
      <div className="intro-row">
        <div>
          <p className="page-kicker">管理员 · 服务配置</p>
          <h2 id="llm-admin-title">LLM API</h2>
          <p className="page-description">供应商名称面向用户展示；每条连接独立配置 URL、Key 和优先级。同供应商的备用连接使用相同路由前缀。</p>
        </div>
        <button className="secondary-button" disabled={loading} onClick={() => void loadProviders()} type="button">
          {loading ? "刷新中…" : "刷新"}
        </button>
      </div>

      {error && <div className="service-page-error" role="alert"><p>{error}</p><button className="secondary-button" onClick={() => void loadProviders()} type="button">重试</button></div>}
      {notice && <p className="form-success llm-feedback" role="status">{notice}</p>}

      <form className="project-detail-panel llm-provider-create" onSubmit={(event) => void importProviders(event)}>
        <div>
          <h3>添加 LLM API 组</h3>
          <p>供应商名称、连接名称、路由前缀和 Base URL 对这一组 API 共用；API Key 文本框每行填写一个密钥。密钥加密保存在服务端，提交后会清空文本框。</p>
        </div>
        <div className="llm-provider-form-grid">
          <label className="project-field"><span>供应商名称</span><input maxLength={100} onChange={(event) => setGroupForm({ ...groupForm, supplier_name: event.target.value })} placeholder="例如：火山引擎" required value={groupForm.supplier_name} /></label>
          <label className="project-field"><span>连接名称</span><input maxLength={100} onChange={(event) => setGroupForm({ ...groupForm, name: event.target.value })} placeholder="例如：北京主账号" required value={groupForm.name} /></label>
          <label className="project-field"><span>模型路由前缀 <small>可选</small></span><input maxLength={64} onChange={(event) => setGroupForm({ ...groupForm, route_prefix: event.target.value })} placeholder="volc" value={groupForm.route_prefix} /></label>
          <label className="project-field"><span>Base URL</span><input maxLength={500} onChange={(event) => setGroupForm({ ...groupForm, base_url: event.target.value })} placeholder="https://api.example.com/v1" required type="url" value={groupForm.base_url} /></label>
        </div>
        <label className="project-field llm-import-field"><span>API Key <small>每行一个，可一次添加多个</small></span><textarea autoComplete="off" disabled={creating} onChange={(event) => setApiKeyText(event.target.value)} placeholder={"API_KEY_1\nAPI_KEY_2\nAPI_KEY_3"} rows={5} spellCheck={false} value={apiKeyText} /></label>
        <div className="llm-import-actions"><span>逐个保存；某个密钥失败不会阻止其他密钥。</span><button className="primary-button" disabled={creating || !apiKeyText.trim()} type="submit">{creating ? "添加中…" : "添加 LLM API"}</button></div>
        {importResults.length > 0 && <ul className="llm-import-results" aria-live="polite">{importResults.map((result) => <li className={result.success ? "llm-import-success" : "llm-import-failure"} key={`${result.line}-${result.message}`}>第 {result.line} 行：{result.message}</li>)}</ul>}
      </form>

      <div className="llm-provider-list-heading"><div><h3>LLM API 列表</h3><p>按供应商、连接名称、路由前缀和 Base URL 合并分组；组内 API Key 可独立查看、测试或管理。优先级数字越小越先尝试。</p></div><span>{providerGroups.length} 组 · {providers.length} 个 API</span></div>
      {loading && providers.length === 0 ? <div className="empty-state">正在加载供应商配置…</div> : null}
      {!loading && !error && providers.length === 0 ? <div className="empty-state">暂无上游连接，请使用上方表单添加。</div> : null}
      <div className="llm-provider-list">
        {providerGroups.map((group) => (
          <article className="project-detail-panel llm-provider-card" key={group.key}>
            <div className="llm-provider-group-heading">
              <div>
                <div className="llm-provider-title"><h3>{group.supplier_name} · {group.name}</h3><span className="llm-key-state">{group.route_prefix ? `前缀 ${group.route_prefix}` : "默认池"} · {group.providers.length} 个 API</span></div>
                <p className="llm-provider-url"><code>{group.base_url}</code></p>
              </div>
            </div>
            <div className="llm-api-key-list">
              {group.providers.map((provider) => (
                <div className="llm-api-key-item" key={provider.id}>
                  <div className="llm-api-key-heading">
                    <div className="llm-api-key-secret"><strong>API {apiNumberById.get(provider.id)}</strong><code>{provider.api_key ?? "密钥不可解密，请检查服务端 LLM_PROVIDER_SECRET_KEY"}</code><button className="secondary-button" disabled={!provider.api_key} onClick={() => void copyApiKey(provider)} type="button">{copiedKeyId === provider.id ? "已复制" : "复制"}</button></div>
                    <div className="llm-provider-actions">
                      <span className={`service-capability-badge ${provider.status === "active" ? "service-capability-enabled" : "service-capability-disabled"}`}>{provider.status === "active" ? "已启用" : "已停用"}</span>
                      <span className="llm-key-state">优先级 {provider.priority}</span>
                      <button className="secondary-button" disabled={busyId === provider.id || !testModels[provider.id]?.trim()} onClick={() => void runTest(provider)} type="button">{busyId === provider.id ? "处理中…" : "测试当前 API"}</button>
                      <button className="secondary-button" disabled={busyId === provider.id} onClick={() => provider.id === editingId ? setEditingId(null) : beginEdit(provider)} type="button">{provider.id === editingId ? "取消编辑" : "编辑"}</button>
                      <button className="secondary-button" disabled={busyId === provider.id} onClick={() => void toggleProvider(provider)} type="button">{provider.status === "active" ? "停用" : "启用"}</button>
                      <button className="danger-button" disabled={busyId === provider.id} onClick={() => void deleteProvider(provider)} type="button">删除</button>
                    </div>
                  </div>
                  <div className="llm-test-status" role="status"><label className="project-field"><span>测试模型名 <small>填供应商原始模型名，不加路由前缀</small></span><input maxLength={200} onChange={(event) => setTestModels((current) => ({ ...current, [provider.id]: event.target.value }))} placeholder="例如：doubao-seed-2-0-lite" value={testModels[provider.id] ?? ""} /></label><span className={provider.last_test_success === true ? "llm-test-ok" : provider.last_test_success === false ? "llm-test-failed" : ""}>{provider.last_test_success === true ? "最近测试成功" : provider.last_test_success === false ? "最近测试失败" : "尚无测试结果"}</span><span>{testedAt(provider.last_tested_at)}</span>{provider.last_test_message && <span>{provider.last_test_message}</span>}</div>
                  {provider.id === editingId && <div className="llm-provider-edit"><label className="project-field"><span>供应商名称</span><input maxLength={100} onChange={(event) => setEditForm({ ...editForm, supplier_name: event.target.value })} value={editForm.supplier_name} /></label><label className="project-field"><span>连接名称</span><input maxLength={100} onChange={(event) => setEditForm({ ...editForm, name: event.target.value })} value={editForm.name} /></label><label className="project-field"><span>模型路由前缀 <small>清空表示移除</small></span><input maxLength={64} onChange={(event) => setEditForm({ ...editForm, route_prefix: event.target.value })} value={editForm.route_prefix} /></label><label className="project-field"><span>Base URL</span><input maxLength={500} onChange={(event) => setEditForm({ ...editForm, base_url: event.target.value })} type="url" value={editForm.base_url} /></label><label className="project-field"><span>轮换 API Key <small>留空保留当前密钥</small></span><input autoComplete="new-password" onChange={(event) => setEditForm({ ...editForm, api_key: event.target.value })} placeholder="输入新密钥以轮换" type="password" value={editForm.api_key} /></label><label className="project-field"><span>优先级 <small>数值越小越优先</small></span><input max="1000000" min="0" onChange={(event) => setEditForm({ ...editForm, priority: event.target.value })} type="number" value={editForm.priority} /></label><button className="primary-button" disabled={busyId === provider.id} onClick={() => void saveProvider(provider)} type="button">保存连接</button></div>}
                </div>
              ))}
            </div>
          </article>
        ))}
      </div>
    </section>
  );
}
