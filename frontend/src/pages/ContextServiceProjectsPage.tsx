import { useCallback, useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { ApiClientError, apiClient, type ContextProjectPage } from "../api/client";
import { useAuth } from "../auth/useAuth";

const PAGE_SIZE = 20;
const contextSections = [
  { path: "templates", label: "提示词模板", description: "维护命名提示词文本" },
  { path: "short", label: "短期记忆", description: "查看会话消息和顺序" },
  { path: "long", label: "长期记忆", description: "管理跨会话的记忆条目" },
  { path: "profile", label: "用户画像", description: "查看项目定义的 JSON 画像" },
];

function getError(error: unknown): string {
  return error instanceof ApiClientError ? error.message : "上下文项目读取失败，请稍后重试。";
}

/** 显示当前用户可访问且已开通上下文服务的项目入口。 */
export function ContextServiceProjectsPage() {
  const { token, user } = useAuth();
  const [page, setPage] = useState<ContextProjectPage | null>(null);
  const [offset, setOffset] = useState(0);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    if (!token) return;
    setLoading(true);
    setError(null);
    try {
      setPage(await apiClient.getContextProjects(token, offset, PAGE_SIZE));
    } catch (reason) {
      setError(getError(reason));
    } finally {
      setLoading(false);
    }
  }, [offset, token]);

  useEffect(() => { void load(); }, [load]);

  return (
    <section className="page-content" aria-labelledby="context-projects-title">
      <div className="intro-row">
        <div>
          <p className="page-kicker">服务 · 项目上下文</p>
          <h2 id="context-projects-title">上下文管理</h2>
          <p className="page-description">选择已开通上下文服务的项目，再进入模板、记忆或画像页面。</p>
        </div>
        <button className="secondary-button" disabled={loading} onClick={() => void load()} type="button">刷新</button>
      </div>
      {error && <p className="form-error" role="alert">{error}</p>}
      {loading && <div className="empty-state">正在读取可访问项目…</div>}
      {!loading && page?.items.length === 0 && (
        <div className="empty-state">当前没有已开通上下文服务的可访问项目。请先在项目详情中申请该服务。</div>
      )}
      <div className="context-service-projects">
        {page?.items.map((project) => (
          <article className="project-detail-panel context-service-project" key={project.id}>
            <div className="context-service-project-heading">
              <div>
                <h3>{project.name}</h3>
                <p>{project.description || "暂无项目描述"}</p>
                <span className="member-empty">{project.owner_id === user?.id ? "我创建的项目" : "我可访问的项目"} · {project.visibility === "public" ? "公开" : "私有"}</span>
              </div>
              <small>项目 ID：{project.id}</small>
            </div>
            <div className="context-service-section-links">
              {contextSections.map((section) => (
                <Link className="context-service-section-link" key={section.path} to={`/services/context/${project.id}/${section.path}`}>
                  <strong>{section.label}</strong>
                  <span>{section.description}</span>
                  <span aria-hidden="true">打开 →</span>
                </Link>
              ))}
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
