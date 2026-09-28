import { useCallback, useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { ApiClientError, apiClient, type Project, type ProjectStatus } from "../api/client";
import { useAuth } from "../auth/useAuth";

function errorMessage(reason: unknown, fallback: string): string {
  return reason instanceof ApiClientError ? reason.message : fallback;
}

function matchesFuzzyTag(tag: string, query: string): boolean {
  const normalizedTag = tag.toLocaleLowerCase();
  const normalizedQuery = query.trim().toLocaleLowerCase();
  if (!normalizedQuery) return true;
  if (normalizedTag.includes(normalizedQuery)) return true;
  let queryIndex = 0;
  for (const character of normalizedTag) {
    if (character === normalizedQuery[queryIndex]) queryIndex += 1;
    if (queryIndex === normalizedQuery.length) return true;
  }
  return false;
}

export function ProjectsPage() {
  const { token, user } = useAuth();
  const [projects, setProjects] = useState<Project[]>([]);
  const [isLoading, setIsLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [queryInput, setQueryInput] = useState("");
  const [query, setQuery] = useState("");
  const [tagFilter, setTagFilter] = useState("");
  const [tagSearch, setTagSearch] = useState("");
  const [isTagMenuOpen, setIsTagMenuOpen] = useState(false);
  const [statusFilter, setStatusFilter] = useState<ProjectStatus | "">("active");
  const [availableTags, setAvailableTags] = useState<string[]>([]);
  const [offset, setOffset] = useState(0);
  const [total, setTotal] = useState(0);
  const pageSize = 20;
  const matchingTags = availableTags
    .filter((tag) => matchesFuzzyTag(tag, tagSearch))
    .sort((left, right) => {
      const query = tagSearch.trim().toLocaleLowerCase();
      const leftStarts = left.startsWith(query);
      const rightStarts = right.startsWith(query);
      return Number(rightStarts) - Number(leftStarts) || left.localeCompare(right);
    })
    .slice(0, 8);

  const loadProjects = useCallback(async () => {
    if (!token) return;
    setIsLoading(true);
    setError(null);
    try {
      const page = await apiClient.getProjects(token, {
        query: query || undefined,
        tag: tagFilter || undefined,
        status: statusFilter || undefined,
        offset,
        limit: pageSize,
      });
      setProjects(page.items);
      setTotal(page.total);
      setAvailableTags(await apiClient.getProjectTags(token));
    } catch (reason) {
      setError(errorMessage(reason, "项目加载失败，请重试。"));
    } finally {
      setIsLoading(false);
    }
  }, [offset, query, statusFilter, tagFilter, token]);

  useEffect(() => {
    void loadProjects();
  }, [loadProjects]);

  return (
    <section className="page-content" aria-labelledby="projects-title">
      <div className="intro-row">
        <div>
          <p className="page-kicker">资源管理</p>
          <h2 id="projects-title">项目</h2>
          <p className="page-description">所有登录用户都可以创建项目；公开项目对所有用户只读开放。</p>
        </div>
        <div className="project-list-actions">
          <button className="secondary-button" onClick={() => void loadProjects()} type="button">刷新</button>
          <Link className="primary-button project-open-button" to="/projects/new">创建项目</Link>
        </div>
      </div>
      {error && <p className="form-error" role="alert">{error}</p>}
      <form className="project-filters" onSubmit={(event) => { event.preventDefault(); setOffset(0); setQuery(queryInput.trim()); }}>
        <input aria-label="搜索项目" onChange={(event) => setQueryInput(event.target.value)} placeholder="搜索项目名称或描述" value={queryInput} />
        <div className="tag-filter-combobox">
          <input
            aria-label="搜索标签"
            aria-autocomplete="list"
            aria-controls="project-tag-options"
            aria-expanded={isTagMenuOpen}
            onChange={(event) => {
              setTagSearch(event.target.value);
              setTagFilter("");
              setIsTagMenuOpen(true);
              setOffset(0);
            }}
            onBlur={(event) => {
              if (!event.currentTarget.parentElement?.contains(event.relatedTarget as Node | null)) {
                setIsTagMenuOpen(false);
              }
            }}
            onFocus={() => setIsTagMenuOpen(true)}
            onKeyDown={(event) => {
              if (event.key === "Escape") setIsTagMenuOpen(false);
              if (event.key === "Enter" && isTagMenuOpen && matchingTags.length > 0) {
                event.preventDefault();
                setTagFilter(matchingTags[0]);
                setTagSearch(matchingTags[0]);
                setIsTagMenuOpen(false);
                setOffset(0);
              }
            }}
            placeholder={tagFilter ? `标签：${tagFilter}` : "搜索标签…"}
            role="combobox"
            value={tagSearch}
          />
          {isTagMenuOpen && <div className="tag-filter-options" id="project-tag-options" role="listbox">
            {matchingTags.length > 0 ? matchingTags.map((tag) => (
              <button
              aria-selected={tagFilter === tag}
              className="tag-filter-option"
              key={tag}
              onClick={() => {
                  setTagFilter(tag);
                  setTagSearch(tag);
                  setIsTagMenuOpen(false);
                  setOffset(0);
                }}
                role="option"
                type="button"
              >
                {tag}
              </button>
            )) : <span className="tag-filter-empty">没有匹配的标签</span>}
          </div>}
        </div>
        <select aria-label="按状态筛选" onChange={(event) => { setStatusFilter(event.target.value as ProjectStatus | ""); setOffset(0); }} value={statusFilter}>
          <option value="">所有状态</option><option value="active">运行中</option><option value="inactive">已停用</option>
        </select>
        <button className="secondary-button" type="submit">搜索</button>
        <button className="secondary-button" onClick={() => { setQueryInput(""); setQuery(""); setTagFilter(""); setTagSearch(""); setIsTagMenuOpen(false); setStatusFilter("active"); setOffset(0); }} type="button">清除筛选</button>
      </form>
      {isLoading ? (
        <div className="empty-state">正在加载项目…</div>
      ) : projects.length === 0 ? (
        <div className="empty-state">暂无可访问项目。</div>
      ) : (
        <div className="project-list">
          {projects.map((project) => (
            <article className="project-card" key={project.id}>
              <div className="project-card-main">
                <div className="project-title-row">
                  <h3>{project.name}</h3>
                  <span className={`project-owner-badge ${project.owner_id === user?.id ? "project-owner-badge-self" : ""}`}>
                    {project.owner_id === user?.id ? "我创建的" : "他人项目"}
                  </span>
                  <span className={`project-status project-status-${project.status}`}>
                    {project.status === "active" ? "运行中" : "已停用"}
                  </span>
                </div>
                <p className="project-description">{project.description || "暂无描述"}</p>
                <div className="project-card-meta">
                  <span className={`project-visibility project-visibility-${project.visibility}`}>
                    {project.visibility === "public" ? "公开" : "私有"}
                  </span>
                  <small>项目 ID：{project.id}</small>
                </div>
                {project.tags.length > 0 && <div className="project-tags">{project.tags.map((tag) => <span className="project-tag" key={tag}>{tag}</span>)}</div>}
              </div>
              <Link className="primary-button project-open-button" to={`/projects/${project.id}`}>
                打开项目
              </Link>
            </article>
          ))}
        </div>
      )}
      {!isLoading && total > pageSize && <nav className="project-pagination" aria-label="项目分页">
        <button className="secondary-button" disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - pageSize))} type="button">上一页</button>
        <span>{offset + 1}–{Math.min(offset + pageSize, total)} / {total}</span>
        <button className="secondary-button" disabled={offset + pageSize >= total} onClick={() => setOffset(offset + pageSize)} type="button">下一页</button>
      </nav>}
    </section>
  );
}
