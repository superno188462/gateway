import { useState, type FormEvent } from "react";
import { Link, useNavigate } from "react-router-dom";
import { ApiClientError, apiClient, type ProjectVisibility } from "../api/client";
import { useAuth } from "../auth/useAuth";

function errorMessage(reason: unknown): string {
  return reason instanceof ApiClientError ? reason.message : "项目创建失败，请重试。";
}

export function ProjectCreatePage() {
  const { token } = useAuth();
  const navigate = useNavigate();
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [tagInput, setTagInput] = useState("");
  const [visibility, setVisibility] = useState<ProjectVisibility>("private");
  const [error, setError] = useState<string | null>(null);
  const [isCreating, setIsCreating] = useState(false);

  async function createProject(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!token || !name.trim() || isCreating) return;
    setIsCreating(true);
    setError(null);
    try {
      const project = await apiClient.createProject(token, {
        name: name.trim(),
        description: description.trim() || undefined,
        visibility,
        tags: tagInput.split(",").map((tag) => tag.trim()).filter(Boolean),
      });
      navigate(`/projects/${project.id}`);
    } catch (reason) {
      setError(errorMessage(reason));
    } finally {
      setIsCreating(false);
    }
  }

  return (
    <section className="page-content" aria-labelledby="project-create-title">
      <Link className="back-link" to="/projects">← 返回项目列表</Link>
      <div className="intro-row project-create-intro">
        <div>
          <p className="page-kicker">资源管理</p>
          <h2 id="project-create-title">创建项目</h2>
          <p className="page-description">创建后你将成为项目所有者，可以继续配置成员和权限。</p>
        </div>
      </div>

      <form className="project-create-card project-create-form-card" onSubmit={(event) => void createProject(event)}>
        <div className="project-create-heading">
          <h3>项目信息</h3>
          <p>项目名称必填；标签最多 5 个，每个不超过 20 个字符。</p>
        </div>
        <div className="project-form project-create-form">
          <label className="project-field project-field-wide">
            <span>项目名称 <b aria-hidden="true">*</b></span>
            <input autoFocus maxLength={100} onChange={(event) => setName(event.target.value)} placeholder="例如：客户支持 Agent" required value={name} />
          </label>
          <label className="project-field project-field-wide">
            <span>项目描述 <small>可选</small></span>
            <textarea maxLength={2000} onChange={(event) => setDescription(event.target.value)} placeholder="简单说明项目用途" rows={4} value={description} />
          </label>
          <label className="project-field">
            <span>项目标签 <small>可选，用逗号分隔</small></span>
            <input onChange={(event) => setTagInput(event.target.value)} placeholder="例如：客服, production" value={tagInput} />
            <small className="project-field-help">标签会自动去空格、转为小写并去重。</small>
          </label>
          <label className="project-field">
            <span>可见范围</span>
            <select onChange={(event) => setVisibility(event.target.value as ProjectVisibility)} value={visibility}>
              <option value="private">私有：仅项目成员可查看</option>
              <option value="public">公开：所有登录用户可只读查看</option>
            </select>
          </label>
        </div>
        {error && <p className="form-error" role="alert">{error}</p>}
        <div className="project-create-actions">
          <Link className="secondary-button" to="/projects">取消</Link>
          <button className="primary-button" disabled={!name.trim() || isCreating} type="submit">
            {isCreating ? "创建中…" : "创建项目"}
          </button>
        </div>
      </form>
    </section>
  );
}
