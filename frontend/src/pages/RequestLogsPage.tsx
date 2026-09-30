import { useCallback, useEffect, useState } from "react";
import type { FormEvent } from "react";
import { Link, useParams } from "react-router-dom";
import { apiClient, ApiClientError, type Project, type RequestLog } from "../api/client";
import { useAuth } from "../auth/useAuth";

function dayValue(date: Date): string {
  return date.toISOString().slice(0, 10);
}

function utcStart(value: string): string {
  return new Date(`${value}T00:00:00.000Z`).toISOString();
}

function utcEndExclusive(value: string): string {
  const date = new Date(`${value}T00:00:00.000Z`);
  date.setUTCDate(date.getUTCDate() + 1);
  return date.toISOString();
}

function formatTokens(value: number): string {
  return new Intl.NumberFormat("zh-CN").format(value);
}

function readableStatus(status: RequestLog["status"]): string {
  if (status === "received") return "处理中";
  return status === "succeeded" ? "成功" : status === "denied" ? "额度/权限拒绝" : "失败";
}

function actorLabel(log: RequestLog): string {
  if (!log.actor_user_id) return log.actor_username ?? "未关联成员";
  return `${log.actor_username ?? "用户已删除"} · ${log.actor_user_id}`;
}

function keyLabel(log: RequestLog): string {
  return log.api_key_id ?? (log.event_type === "service_call" ? "未认证 Key" : "-");
}

function errorMessage(reason: unknown): string {
  if (reason instanceof ApiClientError) return reason.message;
  return reason instanceof Error ? reason.message : "加载失败，请稍后重试。";
}

export function RequestLogsPage() {
  const { token, user } = useAuth();
  const { projectId: routeProjectId } = useParams();
  const isAdmin = user?.role === "admin";
  const initialEnd = dayValue(new Date());
  const initialStartDate = new Date();
  initialStartDate.setUTCDate(initialStartDate.getUTCDate() - 6);
  const initialStart = dayValue(initialStartDate);
  const [projects, setProjects] = useState<Project[]>([]);
  const [startDate, setStartDate] = useState(initialStart);
  const [endDate, setEndDate] = useState(initialEnd);
  const [projectId, setProjectId] = useState(routeProjectId ?? "");
  const [status, setStatus] = useState<RequestLog["status"] | "">("");
  const [serviceCode, setServiceCode] = useState("");
  const [requestId, setRequestId] = useState("");
  const [appliedFilters, setAppliedFilters] = useState({
    startDate: initialStart,
    endDate: initialEnd,
    projectId: routeProjectId ?? "",
    status: "" as RequestLog["status"] | "",
    serviceCode: "",
    requestId: "",
  });
  const [logs, setLogs] = useState<RequestLog[]>([]);
  const [currentPage, setCurrentPage] = useState(1);
  const [totalPages, setTotalPages] = useState(0);
  const [totalCount, setTotalCount] = useState(0);
  const [selectedLog, setSelectedLog] = useState<RequestLog | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!token || (!isAdmin && routeProjectId)) return;
    let cancelled = false;
    void apiClient.getProjects(token, { limit: 100 }).then((page) => {
      if (!cancelled) setProjects(page.items);
    }).catch((reason: unknown) => {
      if (!cancelled) setError(errorMessage(reason));
    });
    return () => { cancelled = true; };
  }, [isAdmin, routeProjectId, token]);

  const loadLogs = useCallback(async (filters: typeof appliedFilters, pageNumber = 1) => {
    if (!token) return;
    setLoading(true);
    setError(null);
    try {
      const query = {
        startAt: utcStart(filters.startDate),
        endAt: utcEndExclusive(filters.endDate),
        ...(filters.status ? { status: filters.status } : {}),
        ...(filters.serviceCode.trim() ? { serviceCode: filters.serviceCode.trim() } : {}),
        ...(filters.requestId.trim() ? { requestId: filters.requestId.trim() } : {}),
        page: pageNumber,
        pageSize: 50,
      };
      const page = isAdmin
        ? await apiClient.getAdminRequestLogs(token, {
            ...query,
            ...(filters.projectId ? { projectId: filters.projectId } : {}),
          })
        : routeProjectId
          ? await apiClient.getProjectRequestLogs(token, filters.projectId, query)
          : await apiClient.getVisibleRequestLogs(token, { ...query, ...(filters.projectId ? { projectId: filters.projectId } : {}) });
      setLogs(page.items);
      setCurrentPage(page.page);
      setTotalPages(page.total_pages);
      setTotalCount(page.total_count);
    } catch (reason) {
      setError(errorMessage(reason));
    } finally {
      setLoading(false);
    }
  }, [isAdmin, routeProjectId, token]);

  useEffect(() => { void loadLogs(appliedFilters, currentPage); }, [appliedFilters, currentPage, loadLogs]);

  useEffect(() => {
    if (routeProjectId && !isAdmin) {
      setProjectId(routeProjectId);
      setCurrentPage(1);
      setAppliedFilters((current) => ({ ...current, projectId: routeProjectId }));
    }
  }, [isAdmin, routeProjectId]);

  async function applyFilters(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setSelectedLog(null);
    setCurrentPage(1);
    setAppliedFilters({ startDate, endDate, projectId, status, serviceCode, requestId });
  }

  async function showDetails(log: RequestLog) {
    if (!token) return;
    setSelectedLog(log);
    try {
      setSelectedLog(isAdmin
        ? await apiClient.getAdminRequestLog(token, log.request_id)
        : routeProjectId
          ? await apiClient.getProjectRequestLog(
            token,
            log.project_id ?? routeProjectId ?? "",
            log.request_id,
          )
          : await apiClient.getVisibleRequestLog(token, log.request_id));
    } catch (reason) {
      setError(errorMessage(reason));
    }
  }

  const isProjectScoped = Boolean(routeProjectId);
  return (
    <section className="page-content" aria-labelledby="request-logs-title">
      <div className="intro-row">
        <div>
          <p className="page-kicker">{isAdmin ? "管理员 · 全局操作日志" : routeProjectId ? "项目空间" : "我可访问的项目"}</p>
          <h2 id="request-logs-title">操作日志</h2>
          <p className="page-description">{isAdmin ? "记录全平台服务调用和项目管理操作。" : routeProjectId ? "记录当前项目的服务调用和管理操作。" : "查看你有权限访问的项目中的服务调用和项目管理操作。"}不记录提示词正文、模型回复或凭据。</p>
        </div>
        <button className="secondary-button" disabled={loading} onClick={() => void loadLogs(appliedFilters, currentPage)} type="button">
          {loading ? "刷新中…" : "刷新"}
        </button>
      </div>

      {error && <p className="form-error" role="alert">{error}</p>}

      <form className="log-filters" onSubmit={(event) => void applyFilters(event)}>
        <label className="project-field"><span>开始日期（UTC）</span><input onChange={(event) => setStartDate(event.target.value)} required type="date" value={startDate} /></label>
        <label className="project-field"><span>结束日期（UTC）</span><input onChange={(event) => setEndDate(event.target.value)} required type="date" value={endDate} /></label>
        {(isAdmin || !routeProjectId) && <label className="project-field"><span>{isAdmin ? "项目" : "可访问项目"}</span><select onChange={(event) => setProjectId(event.target.value)} value={projectId}><option value="">{isAdmin ? "全部项目" : "全部可访问项目"}</option>{projects.map((project) => <option key={project.id} value={project.id}>{project.name}</option>)}</select></label>}
        <label className="project-field"><span>状态</span><select onChange={(event) => setStatus(event.target.value as RequestLog["status"] | "")} value={status}><option value="">全部状态</option><option value="received">处理中</option><option value="succeeded">成功</option><option value="failed">失败</option><option value="denied">权限/额度拒绝</option></select></label>
        <label className="project-field"><span>服务代码</span><input maxLength={50} onChange={(event) => setServiceCode(event.target.value)} placeholder="例如 llm" value={serviceCode} /></label>
        <label className="project-field"><span>Request ID</span><input maxLength={64} onChange={(event) => setRequestId(event.target.value)} placeholder="精确匹配" value={requestId} /></label>
        <button className="primary-button" disabled={loading} type="submit">筛选</button>
      </form>

      <div className="project-detail-panel request-log-panel">
        <div className="usage-panel-heading"><div><h3>操作事件</h3><p>数据库按页查询 · 每页 50 条</p></div><span>共 {formatTokens(totalCount)} 条</span></div>
        {loading && logs.length === 0 ? <div className="empty-state">正在加载操作日志…</div> : logs.length === 0 ? <div className="empty-state">当前筛选范围没有操作事件。</div> : (
          <div className="request-log-table-wrap">
            <table className={`request-log-table${isAdmin ? " request-log-table-admin" : ""}`}>
              <thead><tr><th>时间（UTC）</th>{isAdmin && <th>项目</th>}<th>Trace ID</th><th>类型</th><th>服务 / 操作</th><th>成员</th><th>API Key ID</th><th>状态</th><th>错误诊断</th><th>结果说明</th><th>耗时</th></tr></thead>
              <tbody>{logs.map((log) => <tr className="request-log-row" key={log.request_id} onClick={() => void showDetails(log)} tabIndex={0} onKeyDown={(event) => { if (event.key === "Enter") void showDetails(log); }}><td>{new Date(log.created_at).toLocaleString("zh-CN", { timeZone: "UTC" })}</td>{isAdmin && <td>{log.project_name ?? (log.project_id ? "已删除项目" : "未认证请求")}</td>}<td>{isAdmin ? <Link className="log-trace-link" onClick={(event) => event.stopPropagation()} title={log.trace_id} to={`/admin/system-logs?trace_id=${encodeURIComponent(log.trace_id)}`}><code>{log.trace_id}</code><span>查看技术日志 →</span></Link> : <code title={log.trace_id}>{log.trace_id}</code>}</td><td>{log.event_type === "project_operation" ? "项目操作" : "服务调用"}</td><td><code>{log.event_type === "project_operation" ? "项目管理" : log.service_code}</code></td><td>{actorLabel(log)}</td><td><code>{keyLabel(log)}</code></td><td><span className={`request-status request-status-${log.status}`}>{readableStatus(log.status)}</span></td><td>{log.error_code ? <><code className="log-error-code">{log.error_code}</code><span className="log-error-message">{log.error_message ?? "操作失败"}</span></> : "-"}</td><td>{log.description ?? "-"}</td><td>{formatTokens(log.latency_ms)} ms</td></tr>)}</tbody>
            </table>
          </div>
        )}
        {totalPages > 1 && <nav className="log-pagination" aria-label="操作日志分页"><button className="secondary-button" disabled={loading || currentPage <= 1} onClick={() => setCurrentPage((page) => Math.max(1, page - 1))} type="button">上一页</button><span>第 {currentPage} / {totalPages} 页</span><button className="secondary-button" disabled={loading || currentPage >= totalPages} onClick={() => setCurrentPage((page) => Math.min(totalPages, page + 1))} type="button">下一页</button></nav>}
      </div>

      {selectedLog && (
        <aside className="project-detail-panel request-log-detail" aria-label="操作日志详情">
          <div className="usage-panel-heading"><div><h3>操作详情</h3><p>{selectedLog.request_id}</p></div><button className="secondary-button" onClick={() => setSelectedLog(null)} type="button">关闭</button></div>
          <dl><dt>项目</dt><dd>{selectedLog.project_name ?? (selectedLog.project_id ? "已删除项目" : "未认证请求")} {selectedLog.project_id && `· ${selectedLog.project_id}`}</dd><dt>类型</dt><dd>{selectedLog.event_type === "project_operation" ? "项目操作" : "服务调用"}</dd><dt>成员</dt><dd>{actorLabel(selectedLog)}</dd><dt>API Key ID</dt><dd>{keyLabel(selectedLog)}</dd><dt>服务 / 操作</dt><dd>{selectedLog.event_type === "project_operation" ? "项目管理" : selectedLog.service_code}</dd><dt>操作结果</dt><dd>{readableStatus(selectedLog.status)}</dd><dt>错误码</dt><dd>{selectedLog.error_code ?? "-"}</dd><dt>原因</dt><dd>{selectedLog.error_message ?? (selectedLog.status === "succeeded" ? "-" : "暂未提供更多信息")}</dd><dt>结果说明</dt><dd>{selectedLog.description ?? "-"}</dd><dt>耗时</dt><dd>{formatTokens(selectedLog.latency_ms)} ms</dd><dt>Request ID / Trace ID</dt><dd>{selectedLog.request_id} / {selectedLog.trace_id}</dd><dt>时间（UTC）</dt><dd>{new Date(selectedLog.created_at).toLocaleString("zh-CN", { timeZone: "UTC" })}</dd></dl>
          {isAdmin && <Link className="secondary-button log-correlation-link" to={`/admin/system-logs?trace_id=${encodeURIComponent(selectedLog.trace_id)}`}>查看关联技术日志 →</Link>}
          <p className="verification-note">操作日志包含项目管理事件和服务调用结果；管理员可在技术日志中按 Trace ID 查看认证、额度、路由和上游诊断。不会记录提示词、回复正文或密钥。</p>
        </aside>
      )}

      {isProjectScoped && routeProjectId && <Link className="back-link" to={`/projects/${routeProjectId}`}>← 返回项目</Link>}
    </section>
  );
}
