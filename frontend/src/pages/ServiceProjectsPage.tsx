import { useCallback, useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { ApiClientError, apiClient, type ServiceProjectPage } from "../api/client";
import { useAuth } from "../auth/useAuth";

const PAGE_SIZE = 20;
const LLM_SERVICE_CODE = "mock-llm-v1";

function formatTokens(value: number): string {
  return new Intl.NumberFormat("zh-CN").format(value);
}

export function ServiceProjectsPage() {
  const { token, user } = useAuth();
  const [page, setPage] = useState<ServiceProjectPage | null>(null);
  const [offset, setOffset] = useState(0);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    if (!token) return;
    setLoading(true);
    setError(null);
    try {
      setPage(await apiClient.getServiceProjects(token, LLM_SERVICE_CODE, offset, PAGE_SIZE));
    } catch (reason) {
      setError(reason instanceof ApiClientError ? reason.message : "LLM 服务项目读取失败，请稍后重试。");
    } finally {
      setLoading(false);
    }
  }, [offset, token]);

  useEffect(() => { void load(); }, [load]);

  return (
    <section className="page-content" aria-labelledby="service-projects-title">
      <div className="intro-row">
        <div>
          <p className="page-kicker">服务 · LLM</p>
          <h2 id="service-projects-title">已开通 LLM 服务的项目</h2>
          <p className="page-description">这里只显示你有权限查看且已经申请 LLM 服务的项目。</p>
        </div>
        <button className="secondary-button" disabled={loading} onClick={() => void load()} type="button">{loading ? "刷新中…" : "刷新"}</button>
      </div>
      {error && <p className="form-error" role="alert">{error}</p>}
      {loading && <div className="empty-state">正在读取已开通 LLM 服务的项目…</div>}
      {!loading && page?.items.length === 0 && <div className="empty-state">目前没有已开通 LLM 服务的可访问项目。</div>}
      <div className="context-service-projects">
        {page?.items.map((project) => (
          <article className="project-detail-panel context-service-project" key={project.id}>
            <div className="context-service-project-heading">
              <div>
                <h3>{project.name}</h3>
                <p>{project.description || "暂无项目描述"}</p>
                <span className="member-empty">{project.owner_id === user?.id ? "我创建的项目" : "我可访问的项目"} · {project.visibility === "public" ? "公开" : "私有"}</span>
              </div>
              <Link className="primary-button project-open-button" to={`/projects/${project.id}`}>打开项目</Link>
            </div>
            <div className="service-project-summary-grid">
              <span>项目月额度<strong>{project.monthly_token_limit === null ? "不适用" : `${formatTokens(project.monthly_token_limit)} tokens`}</strong></span>
              <span>本月已用<strong>{formatTokens(project.tokens_used)} tokens</strong></span>
              <span>处理中预留<strong>{formatTokens(project.tokens_reserved)} tokens</strong></span>
              <span>服务状态<strong>{project.service_status === "active" ? "运行中" : "已暂停"}</strong></span>
            </div>
          </article>
        ))}
      </div>
      {page && page.total > PAGE_SIZE && (
        <div className="pagination-row">
          <span>共 {page.total} 个项目 · 第 {Math.floor(offset / PAGE_SIZE) + 1} / {Math.ceil(page.total / PAGE_SIZE)} 页</span>
          <div>
            <button className="secondary-button" disabled={offset === 0 || loading} onClick={() => setOffset(Math.max(0, offset - PAGE_SIZE))} type="button">上一页</button>
            <button className="secondary-button" disabled={offset + PAGE_SIZE >= page.total || loading} onClick={() => setOffset(offset + PAGE_SIZE)} type="button">下一页</button>
          </div>
        </div>
      )}
    </section>
  );
}
