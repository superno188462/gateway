import { useCallback, useEffect, useRef, useState } from "react";
import type { FormEvent } from "react";
import { ApiClientError, apiClient, type AsrProvider } from "../api/client";
import { useAuth } from "../auth/useAuth";

const DEFAULT_RESOURCE_ID = "volc.seedasr.sauc.duration";
const DEFAULT_FILE_URL = "wss://openspeech.bytedance.com/api/v3/plan/sauc/bigmodel_nostream";
const DEFAULT_REALTIME_URL = "wss://openspeech.bytedance.com/api/v3/plan/sauc/bigmodel_async";

const emptyForm = () => ({
  supplier_name: "火山引擎",
  name: "豆包 ASR",
  route_prefix: "volc",
  model_name: "doubao-seed-asr-2.0",
  resource_id: DEFAULT_RESOURCE_ID,
  file_transcription_url: DEFAULT_FILE_URL,
  realtime_url: DEFAULT_REALTIME_URL,
});

function errorMessage(reason: unknown): string {
  if (reason instanceof ApiClientError) return reason.message;
  return reason instanceof Error ? reason.message : "操作失败，请稍后重试。";
}

export function AsrProvidersPage() {
  const { token } = useAuth();
  const [providers, setProviders] = useState<AsrProvider[]>([]);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [busyId, setBusyId] = useState<string | null>(null);
  const [editingId, setEditingId] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [apiKeys, setApiKeys] = useState("");
  const [importResults, setImportResults] = useState<Array<{ line: number; success: boolean; message: string }>>([]);
  const formRef = useRef<HTMLFormElement>(null);
  const [form, setForm] = useState(emptyForm);

  const load = useCallback(async () => {
    if (!token) return;
    setLoading(true);
    setError(null);
    try {
      setProviders(await apiClient.getAsrProviders(token));
    } catch (reason) {
      setError(errorMessage(reason));
    } finally {
      setLoading(false);
    }
  }, [token]);

  useEffect(() => { void load(); }, [load]);

  const providerGroups = new Map<string, {
    supplier_name: string;
    name: string;
    route_prefix: string | null;
    model_name: string;
    resource_id: string;
    file_transcription_url: string;
    realtime_url: string;
    providers: AsrProvider[];
  }>();
  for (const provider of providers) {
    const groupKey = JSON.stringify([
      provider.supplier_name,
      provider.name,
      provider.route_prefix,
      provider.model_name,
      provider.resource_id,
      provider.file_transcription_url.replace(/\/+$/, ""),
      provider.realtime_url.replace(/\/+$/, ""),
    ]);
    const group = providerGroups.get(groupKey) ?? {
      supplier_name: provider.supplier_name,
      name: provider.name,
      route_prefix: provider.route_prefix,
      model_name: provider.model_name,
      resource_id: provider.resource_id,
      file_transcription_url: provider.file_transcription_url,
      realtime_url: provider.realtime_url,
      providers: [],
    };
    group.providers.push(provider);
    providerGroups.set(groupKey, group);
  }

  async function createConnections(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!token || saving) return;
    if (editingId) {
      setSaving(true);
      setError(null);
      setNotice(null);
      try {
        await apiClient.updateAsrProvider(token, editingId, {
          ...form,
          route_prefix: form.route_prefix || null,
          ...(apiKeys.trim() ? { api_key: apiKeys.trim() } : {}),
        });
        setEditingId(null);
        setApiKeys("");
        setNotice("ASR 连接已更新。");
        setForm(emptyForm());
        await load();
      } catch (reason) {
        setError(errorMessage(reason));
      } finally {
        setSaving(false);
      }
      return;
    }
    const keys = apiKeys.split(/\r?\n/).map((value) => value.trim()).filter(Boolean);
    if (!keys.length) {
      setError("请至少填写一个供应商 API Key。每行一个 Key。");
      return;
    }
    setSaving(true);
    setError(null);
    setNotice(null);
    const results: Array<{ line: number; success: boolean; message: string }> = [];
    const seenKeys = new Set<string>();
    const normalizedFileUrl = form.file_transcription_url.trim().replace(/\/+$/, "");
    const normalizedRealtimeUrl = form.realtime_url.trim().replace(/\/+$/, "");
    const resourceId = form.resource_id.trim();
    for (const [index, rawKey] of apiKeys.split(/\r?\n/).entries()) {
      const api_key = rawKey.trim();
      if (!api_key) continue;
      if (providers.some((provider) =>
        provider.resource_id === resourceId
        && provider.file_transcription_url.replace(/\/+$/, "") === normalizedFileUrl
        && provider.realtime_url.replace(/\/+$/, "") === normalizedRealtimeUrl
        && provider.api_key === api_key,
      )) {
        results.push({ line: index + 1, success: false, message: `API Key ${index + 1}：仓库中已有相同 Resource ID、端点和密钥，未重复新增` });
        continue;
      }
      if (seenKeys.has(api_key)) {
        results.push({ line: index + 1, success: false, message: `API Key ${index + 1}：输入内容重复，未保存` });
        continue;
      }
      seenKeys.add(api_key);
      try {
        await apiClient.createAsrProvider(token, {
          ...form,
          supplier_name: form.supplier_name.trim(),
          name: form.name.trim(),
          route_prefix: form.route_prefix.trim() || undefined,
          resource_id: resourceId,
          file_transcription_url: normalizedFileUrl,
          realtime_url: normalizedRealtimeUrl,
          api_key,
        });
        results.push({ line: index + 1, success: true, message: `API Key ${index + 1}：已添加` });
      } catch (reason) {
        results.push({ line: index + 1, success: false, message: `API Key ${index + 1}：${errorMessage(reason)}` });
      }
    }
    setImportResults(results);
    setApiKeys("");
    const successCount = results.filter((result) => result.success).length;
    setNotice(`API 添加完成：成功 ${successCount} 个，失败 ${results.length - successCount} 个。每个 API Key 会独立创建并参与路由。`);
    await load();
    setSaving(false);
  }

  function beginEdit(provider: AsrProvider) {
    setEditingId(provider.id);
    setError(null);
    setNotice(null);
    setApiKeys("");
    setForm({
      supplier_name: provider.supplier_name,
      name: provider.name,
      route_prefix: "volc",
      model_name: provider.model_name,
      resource_id: provider.resource_id,
      file_transcription_url: provider.file_transcription_url,
      realtime_url: provider.realtime_url,
    });
    formRef.current?.scrollIntoView({ behavior: "smooth", block: "center" });
  }

  function cancelEdit() {
    setEditingId(null);
    setApiKeys("");
    setForm(emptyForm());
  }

  async function toggle(provider: AsrProvider) {
    if (!token) return;
    setBusyId(provider.id);
    setError(null);
    try {
      await apiClient.updateAsrProvider(token, provider.id, {
        status: provider.status === "active" ? "disabled" : "active",
      });
      await load();
    } catch (reason) {
      setError(errorMessage(reason));
    } finally {
      setBusyId(null);
    }
  }

  async function remove(provider: AsrProvider) {
    if (!token || !window.confirm(`确认删除 ASR API「${provider.name}」？`)) return;
    setBusyId(provider.id);
    setError(null);
    try {
      await apiClient.deleteAsrProvider(token, provider.id);
      setNotice("ASR API 已删除。");
      await load();
    } catch (reason) {
      setError(errorMessage(reason));
    } finally {
      setBusyId(null);
    }
  }

  return (
    <section className="page-content llm-admin-page" aria-labelledby="asr-provider-title">
      <div className="intro-row">
        <div>
          <p className="page-kicker">管理员 · 服务配置</p>
          <h2 id="asr-provider-title">ASR API</h2>
          <p className="page-description">配置语音识别上游。项目使用 OpenAI Audio Transcriptions 接口和项目 API Key；供应商凭据仅在服务端保存。</p>
        </div>
        <button className="secondary-button" disabled={loading} onClick={() => void load()} type="button">{loading ? "读取中…" : "刷新"}</button>
      </div>
      {error && <p className="form-error llm-feedback" role="alert">{error}</p>}
      {notice && <p className="form-success llm-feedback" role="status">{notice}</p>}
      <form className="surface-card llm-provider-create" onSubmit={(event) => void createConnections(event)} ref={formRef}>
        <div><h3>{editingId ? "编辑 ASR 连接" : "添加 ASR API 组"}</h3><p>{editingId ? "修改此连接配置。API Key 留空会保留当前密钥，输入新 Key 则会轮换密钥。" : "供应商参数与两种调用模式的 URL 由组内 API 共用；API Key 文本框每行填写一个密钥。"}</p></div>
        <div className="llm-provider-form-grid">
          <label className="project-field"><span>供应商</span><select onChange={(event) => setForm({ ...form, supplier_name: event.target.value })} value={form.supplier_name}><option value="火山引擎">火山引擎</option><option disabled value="其他">其他供应商（暂未支持）</option></select></label>
          <label className="project-field"><span>连接名称</span><input maxLength={100} onChange={(event) => setForm({ ...form, name: event.target.value })} required value={form.name} /></label>
          <label className="project-field"><span>服务商</span><input aria-readonly="true" readOnly value="火山引擎" /><small>网关负责供应商协议转换，业务端无需配置供应商前缀。</small></label>
          <label className="project-field"><span>公开模型名</span><input maxLength={200} onChange={(event) => setForm({ ...form, model_name: event.target.value })} placeholder="例如：doubao-seed-asr-2.0" required value={form.model_name} /><small>调用方 start.model 使用此模型名；Resource ID 由网关按连接配置。</small></label>
          <label className="project-field"><span>Resource ID</span><input maxLength={200} onChange={(event) => setForm({ ...form, resource_id: event.target.value })} required value={form.resource_id} /></label>
          <label className="project-field"><span>文件转写 URL</span><input maxLength={500} onChange={(event) => setForm({ ...form, file_transcription_url: event.target.value })} required value={form.file_transcription_url} /><small>录音文件上传后返回完整识别结果，火山推荐 bigmodel_nostream。</small></label>
          <label className="project-field"><span>实时流识别 URL</span><input maxLength={500} onChange={(event) => setForm({ ...form, realtime_url: event.target.value })} required value={form.realtime_url} /><small>用于实时双向音频流，火山推荐 bigmodel_async。</small></label>
        </div>
        <label className="project-field llm-import-field"><span>{editingId ? "更换供应商 API Key（可留空）" : <>供应商 API Key <small>每行一个，可一次添加多个</small></>}</span><textarea autoComplete="off" disabled={saving} onChange={(event) => setApiKeys(event.target.value)} placeholder={editingId ? "留空保留当前密钥；输入新密钥以轮换" : "API_KEY_1\nAPI_KEY_2\nAPI_KEY_3"} rows={5} spellCheck={false} value={apiKeys} /></label>
        <div className="llm-import-actions"><span>当前仅支持火山引擎。项目端传入公开模型名即可；Resource ID 和上游 URL 仅由网关使用。</span><div className="project-detail-actions"><button className="primary-button llm-submit" disabled={saving} type="submit">{saving ? (editingId ? "保存中…" : "添加中…") : editingId ? "保存修改" : "添加连接"}</button>{editingId && <button className="secondary-button" disabled={saving} onClick={cancelEdit} type="button">取消编辑</button>}</div></div>
      </form>
      {importResults.length > 0 && <div className="llm-import-results" aria-live="polite">{importResults.map((result) => <p className={result.success ? "form-success" : "form-error"} key={`${result.line}-${result.message}`}>{result.message}</p>)}</div>}
      <div className="llm-provider-list-heading"><div><h3>ASR 上游 API</h3><p>按供应商、连接名称、Resource ID 和两种模式的 URL 分组；组内 API Key 独立管理，并按顺序故障切换。</p></div><span>{providerGroups.size} 组 · {providers.length} 个 API</span></div>
      {loading && providers.length === 0 && <div className="empty-state">正在加载 ASR 连接…</div>}
      {!loading && providers.length === 0 && <div className="empty-state">暂无 ASR 上游连接。</div>}
      <div className="llm-provider-list">
        {Array.from(providerGroups.entries()).map(([groupKey, group]) => (
          <article className="surface-card llm-provider-card" key={groupKey}>
            <div className="llm-provider-group-heading">
              <div><div className="llm-provider-title"><h3>{group.supplier_name} · {group.name}</h3><span className="llm-key-state">{group.route_prefix ? `前缀 ${group.route_prefix}` : "默认池"} · {group.providers.length} 个 API</span></div>
                <p className="llm-provider-url">公开模型：<code>{group.model_name}</code> · Resource ID：<code>{group.resource_id}</code></p>
                <p className="llm-provider-url">文件转写：<code>{group.file_transcription_url}</code></p>
                <p className="llm-provider-url">实时流识别：<code>{group.realtime_url}</code></p>
              </div>
            </div>
            <div className="llm-api-key-list">
              {group.providers.map((provider, index) => (
                <div className="llm-api-key-item" key={provider.id}>
                  <div className="llm-api-key-heading">
                    <div className="llm-api-key-secret"><strong>API {index + 1}</strong><code>{provider.api_key ?? "密钥不可解密，请检查 LLM_PROVIDER_SECRET_KEY"}</code></div>
                    <div className="llm-provider-actions"><span className={`service-capability-badge ${provider.status === "active" ? "service-capability-enabled" : "service-capability-disabled"}`}>{provider.status === "active" ? "已启用" : "已停用"}</span><button className="secondary-button" disabled={busyId === provider.id} onClick={() => beginEdit(provider)} type="button">编辑</button><button className="secondary-button" disabled={busyId === provider.id} onClick={() => void toggle(provider)} type="button">{provider.status === "active" ? "停用" : "启用"}</button><button className="danger-button" disabled={busyId === provider.id} onClick={() => void remove(provider)} type="button">删除</button></div>
                  </div>
                </div>
              ))}
            </div>
          </article>
        ))}
      </div>
    </section>
  );
}
