import { useCallback, useEffect, useState } from "react";
import { ApiClientError, apiClient, type Project } from "../api/client";
import { useAuth } from "../auth/useAuth";

export function ProjectsPage() {
  const { token, user } = useAuth();
  const [projects, setProjects] = useState<Project[]>([]);
  const [isLoading, setIsLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [isCreating, setIsCreating] = useState(false);

  const loadProjects = useCallback(async () => {
    if (!token) return;
    setIsLoading(true);
    setError(null);
    try {
      setProjects(await apiClient.getProjects(token));
    } catch (reason) {
      setError(reason instanceof ApiClientError ? reason.message : "项目加载失败，请重试。 ");
    } finally {
      setIsLoading(false);
    }
  }, [token]);

  useEffect(() => {
    void loadProjects();
  }, [loadProjects]);

  async function createProject() {
    if (!token || !name.trim() || isCreating) return;
    setIsCreating(true);
    setError(null);
    try {
      const project = await apiClient.createProject(token, {
        name: name.trim(),
        description: description.trim() || undefined,
      });
      setProjects((current) => [project, ...current]);
      setName("");
      setDescription("");
    } catch (reason) {
      setError(reason instanceof ApiClientError ? reason.message : "项目创建失败，请重试。 ");
    } finally {
      setIsCreating(false);
    }
  }

  async function toggleProject(project: Project) {
    if (!token) return;
    try {
      const updated = await apiClient.updateProject(token, project.id, {
        status: project.status === "active" ? "inactive" : "active",
      });
      setProjects((current) => current.map((item) => item.id === updated.id ? updated : item));
    } catch (reason) {
      setError(reason instanceof ApiClientError ? reason.message : "项目状态更新失败，请重试。 ");
    }
  }

  const canCreate = user?.role === "admin";
  return (
    <section className="page-content" aria-labelledby="projects-title">
      <div className="intro-row">
        <div>
          <p className="page-kicker">资源管理</p>
          <h2 id="projects-title">项目</h2>
          <p className="page-description">管理员管理全部项目，普通用户只看到已加入的项目。</p>
        </div>
        <button className="secondary-button" onClick={() => void loadProjects()} type="button">
          刷新
        </button>
      </div>
      {canCreate && (
        <div className="project-create-card">
          <div>
            <h3>创建项目</h3>
            <p>新项目会自动把当前管理员设为 owner。</p>
          </div>
          <div className="project-form">
            <input aria-label="项目名称" onChange={(event) => setName(event.target.value)} placeholder="项目名称" value={name} />
            <input aria-label="项目描述" onChange={(event) => setDescription(event.target.value)} placeholder="项目描述（可选）" value={description} />
            <button className="primary-button" disabled={!name.trim() || isCreating} onClick={() => void createProject()} type="button">
              {isCreating ? "创建中…" : "创建项目"}
            </button>
          </div>
        </div>
      )}
      {error && <p className="form-error" role="alert">{error}</p>}
      {isLoading ? (
        <div className="empty-state">正在加载项目…</div>
      ) : projects.length === 0 ? (
        <div className="empty-state">暂无可访问项目。</div>
      ) : (
        <div className="project-list">
          {projects.map((project) => (
            <article className="project-card" key={project.id}>
              <div>
                <div className="project-title-row">
                  <h3>{project.name}</h3>
                  <span className={`project-status project-status-${project.status}`}>
                    {project.status === "active" ? "运行中" : "已停用"}
                  </span>
                </div>
                <p>{project.description || "暂无描述"}</p>
                <small>创建于 {new Date(project.created_at).toLocaleDateString()}</small>
              </div>
              {user?.role === "admin" && (
                <button className="secondary-button" onClick={() => void toggleProject(project)} type="button">
                  {project.status === "active" ? "停用" : "启用"}
                </button>
              )}
            </article>
          ))}
        </div>
      )}
    </section>
  );
}
