import { useCallback, useEffect, useState } from "react";
import type { FormEvent } from "react";
import { useSearchParams } from "react-router-dom";
import { apiClient, ApiClientError, type TechnicalLogEntry } from "../api/client";
import { useAuth } from "../auth/useAuth";

function explain(reason: unknown): string {
  if (reason instanceof ApiClientError) return reason.message;
  return reason instanceof Error ? reason.message : "读取技术日志失败。";
}

export function SystemLogsPage() {
  const { token } = useAuth();
  const [searchParams] = useSearchParams();
  const initialTraceId = searchParams.get("trace_id") ?? "";
  const [entries, setEntries] = useState<TechnicalLogEntry[]>([]);
  const [logFile, setLogFile] = useState("gateway.log");
  const [level, setLevel] = useState<TechnicalLogEntry["level"] | "">("");
  const [traceId, setTraceId] = useState(initialTraceId);
  const [query, setQuery] = useState("");
  const [applied, setApplied] = useState({ level: "" as TechnicalLogEntry["level"] | "", traceId: initialTraceId, query: "" });
  const [currentPage, setCurrentPage] = useState(1);
  const [totalPages, setTotalPages] = useState(0);
  const [totalCount, setTotalCount] = useState(0);
  const [paginationAvailable, setPaginationAvailable] = useState(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    if (!token) return;
    setLoading(true);
    try {
      const result = await apiClient.getTechnicalLogs(token, {
        ...(applied.level ? { level: applied.level } : {}),
        ...(applied.traceId.trim() ? { traceId: applied.traceId.trim() } : {}),
        ...(applied.query.trim() ? { query: applied.query.trim() } : {}),
        page: currentPage,
        pageSize: 50,
      });
      setEntries(result.entries);
      setLogFile(result.log_file);
      const hasPagination = Number.isInteger(result.page)
        && Number.isInteger(result.page_size)
        && Number.isInteger(result.total_count)
        && Number.isInteger(result.total_pages);
      setPaginationAvailable(hasPagination);
      if (hasPagination) {
        setCurrentPage(result.page);
        setTotalPages(result.total_pages);
        setTotalCount(result.total_count);
        setError(null);
      } else {
        setCurrentPage(1);
        setTotalPages(0);
        setTotalCount(0);
        setError("当前后端尚未返回分页信息。请重启后端（Ctrl+C 后重新运行 uv run python main.py），再刷新日志。");
      }
    } catch (reason) {
      setError(explain(reason));
    } finally {
      setLoading(false);
    }
  }, [applied, currentPage, token]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  function apply(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const next = { level, traceId, query };
    const unchanged = next.level === applied.level && next.traceId === applied.traceId && next.query === applied.query;
    setApplied(next);
    setCurrentPage(1);
    if (unchanged && currentPage === 1) void refresh();
  }

  return (
    <section className="page-content" aria-labelledby="system-logs-title">
      <div className="intro-row">
        <div>
          <p className="page-kicker">管理员 · 运行诊断</p>
          <h2 id="system-logs-title">技术日志</h2>
          <p className="page-description">{logFile} · 打开页面时读取，之后仅在手动刷新时重新读取；只对管理员开放。</p>
        </div>
        <button className="secondary-button" disabled={loading} onClick={() => void refresh()} type="button">
          {loading ? "刷新中…" : "立即刷新"}
        </button>
      </div>
      {error && <p className="form-error" role="alert">{error}</p>}
      <form className="log-filters system-log-filters" onSubmit={apply}>
        <label className="project-field"><span>等级</span><select onChange={(event) => setLevel(event.target.value as TechnicalLogEntry["level"] | "")} value={level}><option value="">全部等级</option><option value="DEBUG">DEBUG</option><option value="INFO">INFO</option><option value="WARNING">WARNING</option><option value="ERROR">ERROR</option><option value="CRITICAL">CRITICAL</option></select></label>
        <label className="project-field"><span>Trace ID</span><input maxLength={64} onChange={(event) => setTraceId(event.target.value)} placeholder="精确或部分匹配" value={traceId} /></label>
        <label className="project-field"><span>事件内容</span><input maxLength={200} onChange={(event) => setQuery(event.target.value)} placeholder="搜索日志消息" value={query} /></label>
        <button className="primary-button" type="submit">筛选</button>
      </form>
      {applied.traceId && <p className="verification-note">当前技术日志已按操作日志 Trace ID 筛选：<code>{applied.traceId}</code></p>}
      <div className="project-detail-panel request-log-panel system-log-panel">
        <div className="usage-panel-heading"><div><h3>运行事件</h3><p>后端按页读取 · 每页 50 条 · 手动刷新</p></div><span>{paginationAvailable ? `共 ${new Intl.NumberFormat("zh-CN").format(totalCount)} 条` : `${entries.length} 条（旧版接口响应）`}</span></div>
        {entries.length === 0 ? <div className="empty-state">{loading ? "正在读取日志…" : "当前日志文件没有匹配记录。"}</div> : (
          <div className="request-log-table-wrap">
            <table className="request-log-table system-log-table">
              <thead><tr><th>时间</th><th>等级</th><th>来源</th><th>Trace ID</th><th>事件</th></tr></thead>
              <tbody>{entries.map((entry, index) => <tr className={`system-log-row system-log-${entry.level.toLowerCase()}`} key={`${entry.timestamp}-${entry.trace_id}-${index}`}><td>{entry.timestamp}</td><td><span className={`log-level log-level-${entry.level.toLowerCase()}`}>{entry.level}</span></td><td><code>{entry.source}</code></td><td><code>{entry.trace_id}</code></td><td className="system-log-message">{entry.message}</td></tr>)}</tbody>
            </table>
          </div>
        )}
        {paginationAvailable && <nav className="log-pagination" aria-label="技术日志分页"><button className="secondary-button" disabled={loading || currentPage <= 1} onClick={() => setCurrentPage((page) => Math.max(1, page - 1))} type="button">上一页</button><span>第 {currentPage} / {Math.max(totalPages, 1)} 页</span><button className="secondary-button" disabled={loading || currentPage >= totalPages} onClick={() => setCurrentPage((page) => Math.min(totalPages, page + 1))} type="button">下一页</button></nav>}
      </div>
    </section>
  );
}
