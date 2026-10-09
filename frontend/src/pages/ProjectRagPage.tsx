import { useCallback, useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { apiClient, type EmbeddingRouteGroup, type RagKnowledgeBase, type RagSearchResponse, type RagVector } from "../api/client";
import { useAuth } from "../auth/useAuth";

export function ProjectRagPage() {
  const { projectId = "" } = useParams<{ projectId: string }>();
  const { token } = useAuth();
  const [bases, setBases] = useState<RagKnowledgeBase[]>([]);
  const [vectors, setVectors] = useState<RagVector[]>([]);
  const [routeGroups, setRouteGroups] = useState<EmbeddingRouteGroup[]>([]);
  const [expandedVectorId, setExpandedVectorId] = useState("");
  const [editingVectorId, setEditingVectorId] = useState("");
  const [vectorContentDraft, setVectorContentDraft] = useState("");
  const [vectorMetadataDraft, setVectorMetadataDraft] = useState("{}");
  const [selectedId, setSelectedId] = useState("");
  const [editingSettings, setEditingSettings] = useState(false);
  const [settingsChunkSize, setSettingsChunkSize] = useState("1000");
  const [settingsRoutePrefix, setSettingsRoutePrefix] = useState("");
  const [canEdit, setCanEdit] = useState(false);
  const [form, setForm] = useState({ name: "", description: "", model: "", maxRecordChars: "1000" });
  const [doc, setDoc] = useState({ title: "", content: "", external_id: "" });
  const [metadataText, setMetadataText] = useState("{}");
  const [query, setQuery] = useState("");
  const [results, setResults] = useState<RagSearchResponse["results"]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");

  const selected = bases.find((base) => base.id === selectedId) ?? null;
  const selectedModel = selected?.model;
  const selectedChunkSize = selected?.chunk_size;
  const load = useCallback(async () => {
    if (!token || !projectId) return;
    setError("");
    try {
      const [permissions, items, groups] = await Promise.all([
        apiClient.getRagPermissions(token, projectId),
        apiClient.getRagKnowledgeBases(token, projectId),
        apiClient.getEmbeddingRouteGroups().catch(() => []),
      ]);
      setCanEdit(permissions.can_edit); setBases(items); setRouteGroups(groups);
      setSelectedId((current) => items.some((item) => item.id === current) ? current : items[0]?.id ?? "");
    } catch (reason) { setError(reason instanceof Error ? reason.message : "读取向量集合失败"); }
  }, [projectId, token]);
  useEffect(() => {
    if (!selectedModel || selectedChunkSize === undefined) return;
    const slash = selectedModel.indexOf("/");
    setSettingsRoutePrefix(slash > 0 ? selectedModel.slice(0, slash) : "");
    setSettingsChunkSize(String(selectedChunkSize));
    setEditingSettings(false);
  }, [selectedId, selectedChunkSize, selectedModel]);
  useEffect(() => { void load(); }, [load]);
  useEffect(() => {
    if (!token || !projectId || !selectedId) { setVectors([]); return; }
    setExpandedVectorId(""); setEditingVectorId("");
    void apiClient.getRagVectors(token, projectId, selectedId).then(setVectors).catch((reason: unknown) => setError(reason instanceof Error ? reason.message : "读取向量记录失败"));
    setResults([]);
  }, [projectId, selectedId, token]);

  async function createBase(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault(); if (!token || busy) return;
    setBusy(true); setError(""); setNotice("");
    try {
      const item = await apiClient.createRagKnowledgeBase(token, projectId, { name: form.name, description: form.description || undefined, model: form.model, chunk_size: Number(form.maxRecordChars), chunk_overlap: 0 });
      setForm({ name: "", description: "", model: "", maxRecordChars: "1000" }); setNotice("向量集合已创建。"); await load(); setSelectedId(item.id);
    } catch (reason) { setError(reason instanceof Error ? reason.message : "创建向量集合失败"); }
    finally { setBusy(false); }
  }
  function editSettings() {
    if (!selected) return;
    const slash = selected.model.indexOf("/");
    setSettingsRoutePrefix(slash > 0 ? selected.model.slice(0, slash) : "");
    setSettingsChunkSize(String(selected.chunk_size));
    setEditingSettings(true);
  }
  async function saveSettings(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault(); if (!token || !selected || busy) return;
    const maxRecordChars = Number(settingsChunkSize);
    if (!Number.isInteger(maxRecordChars) || maxRecordChars < 100 || maxRecordChars > 6000) {
      setError("单条记录最大字符数须在 100 到 6000 之间。"); return;
    }
    const oldSlash = selected.model.indexOf("/");
    const oldPrefix = oldSlash > 0 ? selected.model.slice(0, oldSlash) : "";
    if (oldPrefix !== settingsRoutePrefix && vectors.length > 0 && !window.confirm(
      `此集合已有 ${vectors.length} 条向量。切换路由前缀不会重新生成已有向量；如果旧向量与新上游不兼容，检索效果会下降，需要重新写入向量。仍要保存吗？`,
    )) return;
    setBusy(true); setError(""); setNotice("");
    try {
      await apiClient.updateRagKnowledgeBaseSettings(token, projectId, selected.id, {
        route_prefix: settingsRoutePrefix || null,
        chunk_size: maxRecordChars,
      });
      setEditingSettings(false); setNotice("集合设置已更新。"); await load();
    } catch (reason) { setError(reason instanceof Error ? reason.message : "更新集合设置失败"); }
    finally { setBusy(false); }
  }
  async function createDocument(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault(); if (!token || !selected || busy) return;
    setBusy(true); setError(""); setNotice("");
    try {
      const metadata = JSON.parse(metadataText) as Record<string, unknown>;
      if (!metadata || Array.isArray(metadata) || typeof metadata !== "object") throw new Error("Metadata 必须是 JSON 对象");
      if (doc.content.length > selected.chunk_size) throw new Error(`单条向量记录不能超过 ${selected.chunk_size} 个字符`);
      await apiClient.createRagDocument(token, projectId, selected.id, { title: doc.title, content: doc.content, external_id: doc.external_id || undefined, metadata });
      setDoc({ title: "", content: "", external_id: "" }); setMetadataText("{}"); setNotice("向量记录已写入。"); setVectors(await apiClient.getRagVectors(token, projectId, selected.id));
    }
    catch (reason) { setError(reason instanceof Error ? reason.message : "添加向量记录失败"); }
    finally { setBusy(false); }
  }
  async function search(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault(); if (!token || !selected || busy) return;
    setBusy(true); setError("");
    try { setResults((await apiClient.searchRag(token, projectId, selected.id, query, 5)).results); }
    catch (reason) { setError(reason instanceof Error ? reason.message : "检索失败"); }
    finally { setBusy(false); }
  }
  async function deleteVector(item: RagVector) {
    if (!token || !selected || !window.confirm("删除向量记录“" + item.title + "”第 " + (item.sequence + 1) + " 条？")) return;
    setBusy(true); setError("");
    try { await apiClient.deleteRagVector(token, projectId, selected.id, item.id); setVectors(await apiClient.getRagVectors(token, projectId, selected.id)); }
    catch (reason) { setError(reason instanceof Error ? reason.message : "删除向量记录失败"); }
    finally { setBusy(false); }
  }
  function openVector(item: RagVector) {
    setExpandedVectorId((current) => current === item.id ? "" : item.id);
    setEditingVectorId("");
  }
  function editVector(item: RagVector) {
    setExpandedVectorId(item.id);
    setEditingVectorId(item.id);
    setVectorContentDraft(item.content);
    setVectorMetadataDraft(JSON.stringify(item.metadata, null, 2));
  }
  async function saveVector(item: RagVector) {
    if (!token || !selected || busy) return;
    setBusy(true); setError(""); setNotice("");
    try {
      const metadata = JSON.parse(vectorMetadataDraft) as Record<string, unknown>;
      if (!metadata || Array.isArray(metadata) || typeof metadata !== "object") throw new Error("Metadata 必须是 JSON 对象");
      const updated = await apiClient.updateRagVector(token, projectId, selected.id, item.id, { content: vectorContentDraft, metadata });
      setVectors((current) => current.map((vector) => vector.id === updated.id ? updated : vector));
      setEditingVectorId(""); setNotice("向量记录已更新。");
    } catch (reason) { setError(reason instanceof Error ? reason.message : "更新向量记录失败"); }
    finally { setBusy(false); }
  }
  async function deleteBase() {
    if (!token || !selected || !window.confirm(`删除向量集合“${selected.name}”及其中全部文档和向量？`)) return;
    setBusy(true); setError("");
    try { await apiClient.deleteRagKnowledgeBase(token, projectId, selected.id); setNotice("向量集合已删除。"); await load(); }
    catch (reason) { setError(reason instanceof Error ? reason.message : "删除向量集合失败"); }
    finally { setBusy(false); }
  }

  return <section className="page-content llm-admin-page" aria-labelledby="rag-title">
    <div className="intro-row"><div><p className="page-kicker">项目 · 向量数据库</p><h2 id="rag-title">向量集合与检索</h2><p className="page-description">每次写入一条已切分的文本向量记录，网关为该条记录生成 Embedding 并保存；文档解析和切片由业务项目负责。</p></div><Link className="secondary-button" to={`/projects/${projectId}`}>返回项目</Link></div>
    {error && <p className="form-error" role="alert">{error}</p>}{notice && <p className="form-success" role="status">{notice}</p>}
    {canEdit && <form className="project-detail-panel llm-provider-create" onSubmit={(event) => void createBase(event)}><h3>新建向量集合</h3><p className="page-description">模型名称创建后不可修改，可在集合设置中调整路由前缀。业务项目负责切片，网关按单条记录保存。</p><div className="llm-provider-form-grid"><label className="project-field"><span>名称</span><input required value={form.name} onChange={(event) => setForm({ ...form, name: event.target.value })} /></label><label className="project-field"><span>Embedding 模型（可含路由前缀）</span><input required placeholder="例如 volc/doubao-embedding-vision" value={form.model} onChange={(event) => setForm({ ...form, model: event.target.value })} /></label><label className="project-field"><span>单条记录最大字符数</span><input required type="number" min={100} max={6000} step={1} value={form.maxRecordChars} onChange={(event) => setForm({ ...form, maxRecordChars: event.target.value })} /></label></div><label className="project-field llm-import-field"><span>描述</span><input value={form.description} onChange={(event) => setForm({ ...form, description: event.target.value })} /></label><button className="primary-button llm-submit" disabled={busy} type="submit">创建集合</button></form>}
    <section className="project-detail-panel rag-panel"><div className="llm-provider-list-heading"><div><h3>向量集合</h3><p>按项目隔离，管理权限来自项目成员角色。</p></div><span>{bases.length} 个</span></div>{bases.length === 0 ? <div className="empty-state">暂无向量集合。</div> : <div className="rag-base-list">{bases.map((base) => <button className={`rag-base-item${selectedId === base.id ? " rag-base-active" : ""}`} key={base.id} onClick={() => setSelectedId(base.id)} type="button"><strong>{base.name}</strong><span>{base.model}</span><small>{vectors.length && selectedId === base.id ? vectors.length : "—"} 条向量 · {base.vector_dimensions ?? "待生成"} 维</small></button>)}</div>}</section>
    {selected && <><section className="project-detail-panel rag-panel"><div className="service-allocation-heading"><div><h3>{selected.name}</h3><p>模型 {selected.model} · 单条记录上限 {selected.chunk_size} 字符 · 向量维度 {selected.vector_dimensions ?? "尚未生成"}</p></div>{canEdit && <div className="rag-vector-edit-actions">{!editingSettings && <button className="secondary-button" disabled={busy} onClick={editSettings} type="button">修改集合设置</button>}<button className="danger-button" disabled={busy} onClick={() => void deleteBase()} type="button">删除集合</button></div>}</div>
      {editingSettings && canEdit && <form className="rag-settings-form" onSubmit={(event) => void saveSettings(event)}><h4>集合设置</h4><div className="llm-provider-form-grid"><label className="project-field"><span>模型名称（不可修改）</span><input readOnly value={selected.model.includes("/") ? selected.model.slice(selected.model.indexOf("/") + 1) : selected.model} /></label><label className="project-field"><span>模型路由前缀</span><select value={settingsRoutePrefix} onChange={(event) => setSettingsRoutePrefix(event.target.value)}><option value="">不指定前缀（自动路由）</option>{routeGroups.filter((group) => group.prefix !== null && (group.prefix === (selected.model.includes("/") ? selected.model.slice(0, selected.model.indexOf("/")) : "") || group.connection_count > 0)).map((group) => <option key={group.prefix} value={group.prefix!}>{group.prefix} · {group.suppliers.join("、")}</option>)}{selected.model.includes("/") && !routeGroups.some((group) => group.prefix === selected.model.slice(0, selected.model.indexOf("/"))) && <option value={selected.model.slice(0, selected.model.indexOf("/"))}>{selected.model.slice(0, selected.model.indexOf("/"))} · 当前前缀</option>}</select></label><label className="project-field"><span>单条记录最大字符数</span><input required type="number" min={100} max={6000} step={1} value={settingsChunkSize} onChange={(event) => setSettingsChunkSize(event.target.value)} /></label></div><p className="page-description">字符数上限只影响之后写入或编辑的记录，不会自动修改已有记录。切换路由前缀也不会重新生成已有向量。</p><div className="rag-vector-edit-actions"><button className="primary-button" disabled={busy} type="submit">{busy ? "保存中…" : "保存设置"}</button><button className="secondary-button" disabled={busy} onClick={() => setEditingSettings(false)} type="button">取消</button></div></form>}
      <div className="rag-document-grid"><div><div className="llm-provider-list-heading"><div><h3>向量记录</h3><p>点击记录查看元数据、创建者和时间；有编辑权限时可修改文本和 Metadata。</p></div><span>{vectors.length} 条</span></div>{vectors.map((item) => <article className="rag-document-item rag-vector-item" key={item.id}><div className="rag-vector-main"><button className="rag-vector-toggle" aria-expanded={expandedVectorId === item.id} disabled={editingVectorId === item.id} onClick={() => openVector(item)} type="button"><strong>{item.title} · #{item.sequence + 1}</strong><span>{item.external_id || item.id} · {item.content.length} 字符</span><span className="rag-vector-preview">{item.content}</span></button>{expandedVectorId === item.id && <div className="rag-vector-details"><dl><div><dt>向量 ID</dt><dd><code>{item.id}</code></dd></div><div><dt>来源记录 ID</dt><dd><code>{item.external_id || item.document_id}</code></dd></div><div><dt>来源创建用户 ID</dt><dd><code>{item.created_by_user_id}</code></dd></div><div><dt>创建时间</dt><dd>{new Date(item.created_at).toLocaleString("zh-CN")}</dd></div><div><dt>修改时间</dt><dd>{new Date(item.updated_at).toLocaleString("zh-CN")}</dd></div></dl><h4>Metadata</h4>{editingVectorId === item.id ? <><label className="project-field"><span>文本片段</span><textarea required maxLength={selected.chunk_size} rows={8} value={vectorContentDraft} onChange={(event) => setVectorContentDraft(event.target.value)} /></label><label className="project-field"><span>Metadata（JSON 对象）</span><textarea rows={6} value={vectorMetadataDraft} onChange={(event) => setVectorMetadataDraft(event.target.value)} /></label><p className="page-description">修改文本会重新生成 Embedding，并计入项目用量；仅修改 Metadata 不产生 Embedding 用量。</p><div className="rag-vector-edit-actions"><button className="primary-button" disabled={busy} onClick={() => void saveVector(item)} type="button">{busy ? "保存中…" : "保存修改"}</button><button className="secondary-button" disabled={busy} onClick={() => setEditingVectorId("")} type="button">取消</button></div></> : <><pre className="rag-vector-metadata">{JSON.stringify(item.metadata, null, 2)}</pre><h4>文本片段</h4><p className="rag-vector-content">{item.content}</p></>}</div>}</div>{canEdit && <div className="rag-vector-actions">{expandedVectorId === item.id && editingVectorId !== item.id && <button className="secondary-button" disabled={busy} onClick={() => editVector(item)} type="button">编辑</button>}<button className="danger-button" disabled={busy} onClick={() => void deleteVector(item)} type="button">删除</button></div>}</article>)}{vectors.length === 0 && <div className="empty-state">尚无向量记录。</div>}</div>
        {canEdit && <form onSubmit={(event) => void createDocument(event)}><h3>添加向量记录</h3><p className="page-description">粘贴业务项目已切分好的单个文本片段，最多 {selected.chunk_size} 个字符。</p><label className="project-field"><span>记录名称</span><input required value={doc.title} onChange={(event) => setDoc({ ...doc, title: event.target.value })} /></label><label className="project-field"><span>记录 ID（可选）</span><input value={doc.external_id} onChange={(event) => setDoc({ ...doc, external_id: event.target.value })} /></label><label className="project-field"><span>文本片段</span><textarea required maxLength={selected.chunk_size} rows={8} value={doc.content} onChange={(event) => setDoc({ ...doc, content: event.target.value })} /></label><label className="project-field"><span>Metadata（JSON 对象）</span><textarea rows={4} value={metadataText} onChange={(event) => setMetadataText(event.target.value)} /></label><button className="primary-button" disabled={busy} type="submit">写入单条向量</button></form>}
      </div></section>
      <section className="project-detail-panel rag-panel"><h3>检索测试</h3><p className="page-description">检索会调用 Embedding 服务并计入项目用量。</p><form className="rag-search-form" onSubmit={(event) => void search(event)}><input required value={query} onChange={(event) => setQuery(event.target.value)} placeholder="输入要检索的问题" /><button className="primary-button" disabled={busy} type="submit">检索 Top 5</button></form>{results.map((result) => <article className="rag-result" key={result.chunk_id}><div><strong>{result.title}</strong><span>相似度 {(result.score * 100).toFixed(1)}%</span></div><p>{result.content}</p></article>)}{results.length === 0 && <div className="empty-state">执行检索后将在此显示匹配切片。</div>}</section></>}
  </section>;
}
