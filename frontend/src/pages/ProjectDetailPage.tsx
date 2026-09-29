import { useCallback, useEffect, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import {
  ApiClientError,
  apiClient,
  type Project,
  type ProjectMember,
  type ProjectService,
  type UserServiceQuota,
  type ProjectVisibility,
} from "../api/client";
import { useAuth } from "../auth/useAuth";

type MemberLookup = "username" | "user_id";
type MemberRole = "editor" | "viewer";

function errorMessage(reason: unknown, fallback: string): string {
  return reason instanceof ApiClientError ? reason.message : fallback;
}

export function ProjectDetailPage() {
  const { projectId } = useParams<{ projectId: string }>();
  const navigate = useNavigate();
  const { token, user } = useAuth();
  const [project, setProject] = useState<Project | null>(null);
  const [members, setMembers] = useState<ProjectMember[]>([]);
  const [isLoading, setIsLoading] = useState(true);
  const [isLoadingMembers, setIsLoadingMembers] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [tagInput, setTagInput] = useState("");
  const [visibility, setVisibility] = useState<ProjectVisibility>("private");
  const [isSavingProject, setIsSavingProject] = useState(false);
  const [isDeletingProject, setIsDeletingProject] = useState(false);
  const [memberLookup, setMemberLookup] = useState<MemberLookup>("username");
  const [memberIdentifier, setMemberIdentifier] = useState("");
  const [memberRole, setMemberRole] = useState<MemberRole>("viewer");
  const [isAddingMember, setIsAddingMember] = useState(false);
  const [removingMemberId, setRemovingMemberId] = useState<string | null>(null);
  const [updatingMemberId, setUpdatingMemberId] = useState<string | null>(null);
  const [projectServices, setProjectServices] = useState<ProjectService[]>([]);
  const [serviceQuotas, setServiceQuotas] = useState<UserServiceQuota[]>([]);
  const [isLoadingServices, setIsLoadingServices] = useState(true);
  const [savingServiceCode, setSavingServiceCode] = useState<string | null>(null);
  const [serviceAllocationInputs, setServiceAllocationInputs] = useState<Record<string, string>>({});
  const [serviceError, setServiceError] = useState<string | null>(null);
  const [serviceSuccess, setServiceSuccess] = useState<string | null>(null);

  const loadProject = useCallback(async () => {
    if (!token || !projectId) return;
    setIsLoading(true);
    setError(null);
    try {
      const result = await apiClient.getProject(token, projectId);
      setProject(result);
      setName(result.name);
      setDescription(result.description ?? "");
      setVisibility(result.visibility);
      setTagInput(result.tags.join(", "));
    } catch (reason) {
      setError(errorMessage(reason, "项目加载失败，请返回项目列表重试。"));
    } finally {
      setIsLoading(false);
    }
  }, [projectId, token]);

  const loadMembers = useCallback(async () => {
    if (!token || !projectId) return;
    setIsLoadingMembers(true);
    try {
      setMembers(await apiClient.getProjectMembers(token, projectId));
    } catch (reason) {
      setError(errorMessage(reason, "成员列表加载失败，请重试。"));
    } finally {
      setIsLoadingMembers(false);
    }
  }, [projectId, token]);

  useEffect(() => {
    void loadProject();
    void loadMembers();
  }, [loadMembers, loadProject]);

  useEffect(() => {
    if (!token || !projectId || project?.owner_id !== user?.id) {
      setIsLoadingServices(false);
      return;
    }
    let isCurrent = true;
    setIsLoadingServices(true);
    setServiceError(null);
    void Promise.all([
      apiClient.getProjectServices(token, projectId),
      apiClient.getMyServices(token),
    ]).then(([projectItems, quotaItems]) => {
      if (!isCurrent) return;
      setProjectServices(projectItems);
      setServiceQuotas(quotaItems);
      setServiceAllocationInputs(Object.fromEntries(quotaItems.map((quota) => {
        const allocation = projectItems.find((item) => item.service_code === quota.service_code);
        return [quota.service_code, allocation ? String(allocation.monthly_token_limit) : ""];
      })));
    }).catch((reason: unknown) => {
      if (isCurrent) setServiceError(errorMessage(reason, "服务额度加载失败，请重试。"));
    }).finally(() => {
      if (isCurrent) setIsLoadingServices(false);
    });
    return () => { isCurrent = false; };
  }, [project?.owner_id, projectId, token, user?.id]);

  async function saveProject() {
    if (!token || !projectId || !name.trim() || isSavingProject) return;
    setIsSavingProject(true);
    setError(null);
    try {
      const updated = await apiClient.updateProject(token, projectId, {
        name: name.trim(),
        description: description.trim() || null,
        visibility,
        tags: tagInput.split(",").map((tag) => tag.trim()).filter(Boolean),
      });
      setProject(updated);
      setName(updated.name);
      setDescription(updated.description ?? "");
      setVisibility(updated.visibility);
      setTagInput(updated.tags.join(", "));
    } catch (reason) {
      setError(errorMessage(reason, "项目保存失败，请重试。"));
    } finally {
      setIsSavingProject(false);
    }
  }

  async function toggleProjectStatus() {
    if (!token || !projectId || !project) return;
    setError(null);
    try {
      setProject(await apiClient.updateProject(token, projectId, {
        status: project.status === "active" ? "inactive" : "active",
      }));
    } catch (reason) {
      setError(errorMessage(reason, "项目状态更新失败，请重试。"));
    }
  }

  async function deleteProject() {
    if (!token || !projectId || !project || isDeletingProject) return;
    const confirmed = window.confirm(
      `确定永久删除项目“${project.name}”吗？项目标签和成员关系也会一并删除，此操作无法撤销。`,
    );
    if (!confirmed) return;
    setIsDeletingProject(true);
    setError(null);
    try {
      await apiClient.deleteProject(token, projectId);
      navigate("/projects", { replace: true });
    } catch (reason) {
      setError(errorMessage(reason, "项目删除失败，请重试。"));
      setIsDeletingProject(false);
    }
  }

  async function addMember() {
    if (!token || !projectId || !memberIdentifier.trim() || isAddingMember) return;
    setIsAddingMember(true);
    setError(null);
    try {
      const member = await apiClient.addProjectMember(token, projectId, {
        [memberLookup]: memberIdentifier.trim(),
        role: memberRole,
      });
      setMembers((current) => [...current, member]);
      setMemberIdentifier("");
    } catch (reason) {
      setError(errorMessage(reason, "添加成员失败，请检查用户名或用户 ID。"));
    } finally {
      setIsAddingMember(false);
    }
  }

  async function updateMemberRole(userId: string, role: MemberRole) {
    if (!token || !projectId || updatingMemberId) return;
    setUpdatingMemberId(userId);
    setError(null);
    try {
      const updated = await apiClient.updateProjectMemberRole(token, projectId, userId, role);
      setMembers((current) => current.map((member) => member.user_id === userId ? updated : member));
    } catch (reason) {
      setError(errorMessage(reason, "修改成员权限失败，请重试。"));
    } finally {
      setUpdatingMemberId(null);
    }
  }

  async function removeMember(userId: string) {
    if (!token || !projectId || removingMemberId) return;
    setRemovingMemberId(userId);
    setError(null);
    try {
      await apiClient.removeProjectMember(token, projectId, userId);
      setMembers((current) => current.filter((member) => member.user_id !== userId));
    } catch (reason) {
      setError(errorMessage(reason, "移除成员失败，请重试。"));
    } finally {
      setRemovingMemberId(null);
    }
  }

  async function saveProjectService(quota: UserServiceQuota) {
    if (!token || !projectId || savingServiceCode) return;
    const monthlyTokenLimit = Number(serviceAllocationInputs[quota.service_code]);
    if (!Number.isSafeInteger(monthlyTokenLimit) || monthlyTokenLimit < 1) {
      setServiceError("请输入大于 0 的整数 token 上限。");
      return;
    }
    const existing = projectServices.find((item) => item.service_code === quota.service_code);
    const maximum = quota.available_tokens + (existing?.monthly_token_limit ?? 0);
    if (monthlyTokenLimit > maximum) {
      setServiceError(`此项目最多可分配 ${new Intl.NumberFormat("zh-CN").format(maximum)} tokens；请先在个人服务页增加可用额度或调整其他项目。`);
      return;
    }
    setSavingServiceCode(quota.service_code);
    setServiceError(null);
    setServiceSuccess(null);
    try {
      const updated = existing
        ? await apiClient.updateProjectServiceAllocation(token, projectId, quota.service_code, monthlyTokenLimit)
        : await apiClient.applyProjectService(token, projectId, {
            service_code: quota.service_code,
            monthly_token_limit: monthlyTokenLimit,
          });
      setProjectServices((current) => [
        ...current.filter((item) => item.service_code !== updated.service_code),
        updated,
      ]);
      setServiceSuccess(existing ? "项目额度已更新。" : "服务已申请并开通。" );
      try {
        setServiceQuotas(await apiClient.getMyServices(token));
      } catch {
        setServiceError("额度已保存，但个人额度汇总未能刷新，请稍后手动刷新页面。");
      }
    } catch (reason) {
      setServiceError(errorMessage(reason, "项目服务额度保存失败，请重试。"));
    } finally {
      setSavingServiceCode(null);
    }
  }

  const canManage = project?.owner_id === user?.id;
  const canViewApiKeys = canManage || members.some(
    (member) => member.user_id === user?.id && member.role === "editor",
  );
  return (
    <section className="page-content" aria-labelledby="project-detail-title">
      <Link className="back-link" to="/projects">← 返回项目列表</Link>
      {isLoading ? (
        <div className="empty-state">正在加载项目…</div>
      ) : project ? (
        <>
          <div className="intro-row project-detail-intro">
            <div>
              <p className="page-kicker">项目空间</p>
              <div className="project-title-row">
                <h2 id="project-detail-title">{project.name}</h2>
                <span className={`project-owner-badge ${project.owner_id === user?.id ? "project-owner-badge-self" : ""}`}>
                  {project.owner_id === user?.id ? "我创建的" : "他人项目"}
                </span>
                <span className={`project-status project-status-${project.status}`}>
                  {project.status === "active" ? "运行中" : "已停用"}
                </span>
                <span className={`project-visibility project-visibility-${project.visibility}`}>
                  {project.visibility === "public" ? "公开" : "私有"}
                </span>
              </div>
              <p className="page-description">{project.description || "暂无项目描述"}</p>
            </div>
            {canManage && <div className="project-detail-actions">
              <button className="secondary-button" onClick={() => void toggleProjectStatus()} type="button">
                {project.status === "active" ? "停用项目" : "启用项目"}
              </button>
              <button className="danger-button" disabled={isDeletingProject} onClick={() => void deleteProject()} type="button">
                {isDeletingProject ? "删除中…" : "删除项目"}
              </button>
            </div>}
          </div>
          {error && <p className="form-error" role="alert">{error}</p>}
          <div className="project-detail-meta">
            <span>项目 ID：<code>{project.id}</code></span>
            <span>所有者 ID：<code>{project.owner_id}</code></span>
            <span>创建于 {new Date(project.created_at).toLocaleString()}</span>
          </div>
          {canViewApiKeys && (
            <div className="project-detail-shortcuts">
              <Link className="secondary-button project-key-link" to={`/projects/${project.id}/keys`}>
                {canManage ? "管理 API Key" : "查看 API Key"} <span aria-hidden="true">→</span>
              </Link>
              <span>owner 可创建和撤销；editor 可查看密钥信息。</span>
            </div>
          )}
          <div className="project-detail-shortcuts">
            <Link className="secondary-button project-key-link" to={`/projects/${project.id}/logs`}>
              查看调用日志 <span aria-hidden="true">→</span>
            </Link>
            <span>仅包含该项目的调用状态、用量和错误分类。</span>
          </div>
          {canManage && (
            <section className="project-detail-panel service-allocation-panel" aria-labelledby="project-service-title">
              <div className="service-allocation-heading">
                <div>
                  <h3 id="project-service-title">服务与额度</h3>
                  <p>从个人月度额度中为此项目分配 token；所有项目共享个人总额度。</p>
                </div>
                <Link className="secondary-button" to="/account/services">查看个人额度</Link>
              </div>
              {serviceError && <p className="form-error" role="alert">{serviceError}</p>}
              {serviceSuccess && <p className="form-success" role="status">{serviceSuccess}</p>}
              {serviceQuotas.some((quota) =>
                quota.allocated_tokens > quota.monthly_token_limit
                || (quota.monthly_token_limit > 0
                  && quota.tokens_used + quota.tokens_reserved >= quota.monthly_token_limit)
              ) && (
                <p className="service-quota-warning" role="alert">
                  项目分配或当月用量已达到/超过个人总额度，个人额度会限制所有项目的实际调用。请查看个人服务页并联系管理员或协调项目额度。
                </p>
              )}
              {isLoadingServices ? (
                <p className="member-empty" aria-live="polite">正在加载服务额度…</p>
              ) : serviceQuotas.length === 0 ? (
                <div className="empty-state service-project-empty">
                  当前没有可申请的服务额度，请联系管理员开通。<Link to="/account/services">查看我的服务</Link>
                </div>
              ) : (
                <div className="project-service-list">
                  {serviceQuotas.map((quota) => {
                    const allocation = projectServices.find((item) => item.service_code === quota.service_code);
                    const maximum = quota.available_tokens + (allocation?.monthly_token_limit ?? 0);
                    const isSaving = savingServiceCode === quota.service_code;
                    return (
                      <div className="project-service-row" key={quota.service_code}>
                        <div className="project-service-summary">
                        <strong>{quota.name}</strong>
                          <span>
                            {allocation ? "已开通" : "未申请"} · {quota.monthly_token_limit === 0
                              ? "个人额度为 0，需联系管理员开通"
                              : `个人可分配 ${new Intl.NumberFormat("zh-CN").format(maximum)} tokens`}
                          </span>
                        </div>
                        <label className="project-service-limit">
                          <span>项目月上限</span>
                          <input
                            aria-label={`${quota.name} 项目月 token 上限`}
                            disabled={!allocation && quota.available_tokens < 1}
                            inputMode="numeric"
                            min={1}
                            onChange={(event) => setServiceAllocationInputs((current) => ({
                              ...current,
                              [quota.service_code]: event.target.value,
                            }))}
                            placeholder="输入 token 数"
                            type="number"
                            value={serviceAllocationInputs[quota.service_code] ?? ""}
                          />
                        </label>
                        <button
                          className="primary-button"
                          disabled={isSaving || (!allocation && quota.available_tokens < 1)}
                          onClick={() => void saveProjectService(quota)}
                          type="button"
                        >
                          {isSaving ? "保存中…" : allocation ? "调整额度" : "申请服务"}
                        </button>
                        {allocation && (
                          <small className="project-service-usage">
                            本月已用 {new Intl.NumberFormat("zh-CN").format(allocation.tokens_used)} · 处理中预留 {new Intl.NumberFormat("zh-CN").format(allocation.tokens_reserved)} tokens
                          </small>
                        )}
                      </div>
                    );
                  })}
                </div>
              )}
            </section>
          )}
          {canManage && (
            <section className="project-detail-panel" aria-labelledby="project-edit-title">
              <h3 id="project-edit-title">项目设置</h3>
              <div className="project-form project-edit-form">
                <input aria-label="项目名称" onChange={(event) => setName(event.target.value)} value={name} />
                <input aria-label="项目描述" onChange={(event) => setDescription(event.target.value)} placeholder="项目描述（可选）" value={description} />
                <select aria-label="项目可见范围" onChange={(event) => setVisibility(event.target.value as ProjectVisibility)} value={visibility}>
                  <option value="private">私有：仅成员可查看</option>
                  <option value="public">公开：所有用户只读查看</option>
                </select>
                <input aria-label="项目标签" onChange={(event) => setTagInput(event.target.value)} placeholder="标签，逗号分隔（最多 5 个）" value={tagInput} />
                <button className="primary-button" disabled={!name.trim() || isSavingProject} onClick={() => void saveProject()} type="button">
                  {isSavingProject ? "保存中…" : "保存修改"}
                </button>
              </div>
            </section>
          )}
          <section className="project-detail-panel member-detail-panel" aria-labelledby="member-title">
            <div className="member-panel-heading">
              <div>
                <h3 id="member-title">项目成员</h3>
                <p>{canManage ? "添加成员并调整项目角色。" : "查看此项目的成员及其角色。"}</p>
              </div>
              <span className="member-count">{members.length} 位成员</span>
            </div>
            {canManage && (
              <div className="project-form member-form">
                <select aria-label="用户查找方式" onChange={(event) => setMemberLookup(event.target.value as MemberLookup)} value={memberLookup}>
                  <option value="username">用户名</option>
                  <option value="user_id">用户 ID</option>
                </select>
                <input
                  aria-label={memberLookup === "username" ? "用户名" : "用户 ID"}
                  onChange={(event) => setMemberIdentifier(event.target.value)}
                  placeholder={memberLookup === "username" ? "输入用户名" : "输入用户 UUID"}
                  value={memberIdentifier}
                />
                <select aria-label="项目角色" onChange={(event) => setMemberRole(event.target.value as MemberRole)} value={memberRole}>
                  <option value="viewer">viewer（只读）</option>
                  <option value="editor">editor（当前与只读权限相同）</option>
                </select>
                <button className="primary-button" disabled={!memberIdentifier.trim() || isAddingMember} onClick={() => void addMember()} type="button">
                  {isAddingMember ? "添加中…" : "添加成员"}
                </button>
              </div>
            )}
            {isLoadingMembers ? (
              <p className="member-empty">正在加载成员…</p>
            ) : members.length === 0 ? (
              <p className="member-empty">暂无成员信息。</p>
            ) : (
              <div className="member-list">
                {members.map((member) => (
                  <div className="member-row" key={member.user_id}>
                    <div className="member-identity">
                      <strong>{member.username}</strong>
                      <code>{member.user_id}</code>
                    </div>
                    {member.role === "owner" || !canManage ? (
                      <span className="member-role">{member.role}</span>
                    ) : (
                      <select
                        aria-label={`修改 ${member.username} 的项目角色`}
                        className="member-role-select"
                        disabled={updatingMemberId !== null}
                        onChange={(event) => void updateMemberRole(member.user_id, event.target.value as MemberRole)}
                        value={member.role}
                      >
                        <option value="viewer">viewer</option>
                        <option value="editor">editor</option>
                      </select>
                    )}
                    {canManage && member.role !== "owner" && (
                      <button
                        className="secondary-button"
                        disabled={removingMemberId === member.user_id}
                        onClick={() => void removeMember(member.user_id)}
                        type="button"
                      >
                        {removingMemberId === member.user_id ? "移除中…" : "移除"}
                      </button>
                    )}
                  </div>
                ))}
              </div>
            )}
          </section>
        </>
      ) : (
        <div className="empty-state">{error || "项目不存在或你没有访问权限。"}</div>
      )}
    </section>
  );
}
