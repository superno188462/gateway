import { useCallback, useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { apiClient, ApiClientError, type LogRetentionRun, type Project, type RequestUsageSummary } from "../api/client";
import { useAuth } from "../auth/useAuth";

function dayValue(date: Date): string {
  return date.toISOString().slice(0, 10);
}

function dateStart(value: string): string {
  return new Date(`${value}T00:00:00.000Z`).toISOString();
}

function dateEndExclusive(value: string): string {
  const end = new Date(`${value}T00:00:00.000Z`);
  end.setUTCDate(end.getUTCDate() + 1);
  return end.toISOString();
}

function formatTokens(value: number): string {
  return new Intl.NumberFormat("zh-CN").format(value);
}

function errorMessage(reason: unknown): string {
  if (reason instanceof ApiClientError) return reason.message;
  return reason instanceof Error ? reason.message : "加载失败，请稍后重试。";
}

export function DashboardPage() {
  const { token, user } = useAuth();
  const today = new Date();
  const weekAgo = new Date(today);
  weekAgo.setUTCDate(weekAgo.getUTCDate() - 6);
  const [startDate, setStartDate] = useState(dayValue(weekAgo));
  const [endDate, setEndDate] = useState(dayValue(today));
  const [projectId, setProjectId] = useState("");
  const [projects, setProjects] = useState<Project[]>([]);
  const [summary, setSummary] = useState<RequestUsageSummary | null>(null);
  const [retention, setRetention] = useState<LogRetentionRun | null>(null);
  const [loading, setLoading] = useState(true);
  const [runningRetention, setRunningRetention] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const load = useCallback(async () => {
    if (!token) return;
    setLoading(true);
    setError(null);
    try {
      const [projectPage, usage] = await Promise.all([
        apiClient.getProjects(token, { limit: 100 }),
        apiClient.getRequestUsageSummary(token, {
          startAt: dateStart(startDate),
          endAt: dateEndExclusive(endDate),
          ...(projectId ? { projectId } : {}),
        }),
      ]);
      setProjects(projectPage.items);
      setSummary(usage);
      if (user?.role === "admin") {
        setRetention(await apiClient.getLogRetentionRun(token));
      }
    } catch (reason) {
      setError(errorMessage(reason));
    } finally {
      setLoading(false);
    }
  }, [endDate, projectId, startDate, token, user?.role]);

  useEffect(() => { void load(); }, [load]);

  async function runRetention() {
    if (!token || runningRetention) return;
    if (!window.confirm("将删除所有早于 30 天且不属于全系统最新 10,000 条的操作日志，确定继续吗？")) return;
    setRunningRetention(true);
    setError(null);
    setNotice(null);
    try {
      const result = await apiClient.runLogRetention(token);
      setRetention(result);
      setNotice(`保留任务完成，清理了 ${formatTokens(result.deleted_count)} 条日志。`);
    } catch (reason) {
      setError(errorMessage(reason));
    } finally {
      setRunningRetention(false);
    }
  }

  const maxDaily = Math.max(1, ...(summary?.by_day.map((item) => item.request_count) ?? []));

  return (
    <section className="page-content" aria-labelledby="dashboard-title">
      <div className="intro-row">
        <div>
          <p className="page-kicker">平台概览</p>
          <h2 id="dashboard-title">调用仪表盘</h2>
          <p className="page-description">用量汇总与请求日志使用同一批网关请求记录实时聚合。</p>
        </div>
        <button className="secondary-button" disabled={loading} onClick={() => void load()} type="button">
          {loading ? "刷新中…" : "刷新"}
        </button>
      </div>

      {error && <p className="form-error" role="alert">{error}</p>}
      {notice && <p className="form-success" role="status">{notice}</p>}

      <div className="log-filters dashboard-filters">
        <label className="project-field"><span>开始日期（UTC）</span><input onChange={(event) => setStartDate(event.target.value)} type="date" value={startDate} /></label>
        <label className="project-field"><span>结束日期（UTC）</span><input onChange={(event) => setEndDate(event.target.value)} type="date" value={endDate} /></label>
        <label className="project-field"><span>项目</span><select onChange={(event) => setProjectId(event.target.value)} value={projectId}><option value="">全部可访问项目</option>{projects.map((project) => <option key={project.id} value={project.id}>{project.name}</option>)}</select></label>
      </div>

      {summary && (
        <>
          <div className="usage-stat-grid">
            <article className="project-detail-panel usage-stat"><span>请求总数</span><strong>{formatTokens(summary.request_count)}</strong><small>成功 {formatTokens(summary.succeeded_count)} · 失败 {formatTokens(summary.failed_count)} · 拒绝 {formatTokens(summary.denied_count)}</small></article>
            <article className="project-detail-panel usage-stat"><span>总 Token</span><strong>{formatTokens(summary.total_tokens)}</strong><small>输入 {formatTokens(summary.prompt_tokens)} · 输出 {formatTokens(summary.completion_tokens)}</small></article>
            <article className="project-detail-panel usage-stat"><span>平均延迟</span><strong>{formatTokens(summary.average_latency_ms)} ms</strong><small>按筛选窗口内所有请求计算</small></article>
            <article className="project-detail-panel usage-stat"><span>费用（人民币）</span><strong>{summary.cost_cny === null ? "-" : summary.cost_cny}</strong><small>尚未配置模型价格</small></article>
          </div>

          <div className="usage-dashboard-grid">
            <section className="project-detail-panel usage-dashboard-panel">
              <div className="usage-panel-heading"><div><h3>每日请求趋势</h3><p>按 UTC 日期统计</p></div><Link to={projectId ? `/projects/${projectId}/logs` : "/logs"}>查看日志 →</Link></div>
              {summary.by_day.length === 0 ? <p className="empty-state">当前时间范围暂无调用记录。</p> : (
                <div className="usage-daily-list">{summary.by_day.map((day) => <div className="usage-daily-row" key={day.day}><time>{new Date(day.day).toLocaleDateString("zh-CN", { timeZone: "UTC", month: "2-digit", day: "2-digit" })}</time><div className="usage-daily-track" role="img" aria-label={`${day.request_count} 次请求`}><span style={{ width: `${Math.max(2, day.request_count / maxDaily * 100)}%` }} /></div><strong>{formatTokens(day.request_count)} 次</strong><small>{formatTokens(day.total_tokens)} tokens</small></div>)}</div>
              )}
            </section>
            <section className="project-detail-panel usage-dashboard-panel">
              <div className="usage-panel-heading"><div><h3>模型用量</h3><p>按总 Token 从高到低</p></div></div>
              {summary.by_model.length === 0 ? <p className="empty-state">当前时间范围暂无模型用量。</p> : <div className="usage-model-list">{summary.by_model.map((model) => <div className="usage-model-row" key={model.model}><code>{model.model}</code><span>{formatTokens(model.request_count)} 次请求</span><strong>{formatTokens(model.total_tokens)} tokens</strong></div>)}</div>}
            </section>
          </div>
        </>
      )}

      {user?.role === "admin" && (
        <section className="project-detail-panel retention-panel">
          <div><h3>日志保留任务</h3><p>保留最近 30 天全部日志，并确保全系统最新 10,000 条日志始终保留。</p><small>{retention ? `最近状态：${retention.status} · ${new Date(retention.started_at).toLocaleString("zh-CN")} · 清理 ${formatTokens(retention.deleted_count)} 条` : "尚未执行过日志清理"}</small></div>
          <button className="secondary-button" disabled={runningRetention} onClick={() => void runRetention()} type="button">{runningRetention ? "执行中…" : "立即清理"}</button>
        </section>
      )}
    </section>
  );
}
