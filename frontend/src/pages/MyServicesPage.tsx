import { useCallback, useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { ApiClientError, apiClient, type LlmProviderCatalog, type UserServiceQuota } from "../api/client";
import { useAuth } from "../auth/useAuth";

type QuotaLookupMode = "username" | "user_id";

function formatTokens(value: number): string {
  return new Intl.NumberFormat("zh-CN").format(value);
}

function errorMessage(reason: unknown): string {
  return reason instanceof ApiClientError ? reason.message : "服务额度加载失败，请检查网络后重试。";
}

export function MyServicesPage() {
  const { token, user } = useAuth();
  const [services, setServices] = useState<UserServiceQuota[]>([]);
  const [providerCatalog, setProviderCatalog] = useState<LlmProviderCatalog | null>(null);
  const [isLoading, setIsLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [lookupMode, setLookupMode] = useState<QuotaLookupMode>("username");
  const [lookupValue, setLookupValue] = useState("");
  const [targetUser, setTargetUser] = useState<{ id: string; username: string; role: "admin" | "user" } | null>(null);
  const [targetQuota, setTargetQuota] = useState<UserServiceQuota | null>(null);
  const [targetQuotas, setTargetQuotas] = useState<UserServiceQuota[]>([]);
  const [quotaInput, setQuotaInput] = useState("");
  const [isLookingUp, setIsLookingUp] = useState(false);
  const [isSavingQuota, setIsSavingQuota] = useState(false);
  const [adminError, setAdminError] = useState<string | null>(null);
  const [adminSuccess, setAdminSuccess] = useState<string | null>(null);

  const loadServices = useCallback(async () => {
    if (!token) return;
    setIsLoading(true);
    setError(null);
    try {
      const [quotaResult, catalogResult] = await Promise.allSettled([
        apiClient.getMyServices(token),
        apiClient.getLlmProviderCatalog(token),
      ]);
      if (quotaResult.status === "rejected") throw quotaResult.reason;
      setServices(quotaResult.value);
      setProviderCatalog(catalogResult.status === "fulfilled" ? catalogResult.value : null);
    } catch (reason) {
      setError(errorMessage(reason));
    } finally {
      setIsLoading(false);
    }
  }, [token]);

  useEffect(() => {
    void loadServices();
  }, [loadServices]);

  async function lookupUser() {
    if (!token || !lookupValue.trim() || isLookingUp) return;
    setIsLookingUp(true);
    setAdminError(null);
    setAdminSuccess(null);
    setTargetUser(null);
    setTargetQuota(null);
    try {
      const search = lookupMode === "username"
        ? { username: lookupValue.trim() }
        : { user_id: lookupValue.trim() };
      const found = await apiClient.lookupUserForQuota(token, search);
      const quotas = await apiClient.getUserServicesForAdmin(token, found.id);
      const defaultQuota = quotas.find((item) => item.service_code === "mock-llm-v1") ?? quotas[0];
      if (!defaultQuota) throw new Error("该用户当前没有可管理的服务额度。");
      setTargetUser(found);
      setTargetQuotas(quotas);
      setTargetQuota(defaultQuota);
      setQuotaInput(String(defaultQuota.monthly_token_limit));
    } catch (reason) {
      setAdminError(reason instanceof Error ? reason.message : "用户查找失败，请重试。");
    } finally {
      setIsLookingUp(false);
    }
  }

  async function saveTargetQuota() {
    if (!token || !targetUser || !targetQuota || isSavingQuota) return;
    const monthlyTokenLimit = Number(quotaInput);
    if (!Number.isSafeInteger(monthlyTokenLimit) || monthlyTokenLimit < 0) {
      setAdminError("请输入大于或等于 0 的整数月额度。");
      return;
    }
    setIsSavingQuota(true);
    setAdminError(null);
    setAdminSuccess(null);
    try {
      const updated = await apiClient.setUserServiceQuota(
        token,
        targetUser.id,
        targetQuota.service_code,
        monthlyTokenLimit,
      );
      setTargetQuota(updated);
      setQuotaInput(String(updated.monthly_token_limit));
      setAdminSuccess(`已更新 ${targetUser.username} 的${targetQuota.name}月额度。`);
      if (targetUser.id === user?.id) {
        try {
          setServices(await apiClient.getMyServices(token));
        } catch {
          setAdminError("额度已修改，但当前页面的个人额度摘要未能刷新。");
        }
      }
    } catch (reason) {
      setAdminError(reason instanceof Error ? reason.message : "额度修改失败，请重试。");
    } finally {
      setIsSavingQuota(false);
    }
  }

  return (
    <section className="page-content" aria-labelledby="my-services-title">
      <div className="intro-row">
        <div>
          <p className="page-kicker">账户</p>
          <h2 id="my-services-title">我的服务</h2>
          <p className="page-description">查看每月可用额度、项目分配和本月用量。</p>
        </div>
        <button className="secondary-button" disabled={isLoading} onClick={() => void loadServices()} type="button">
          {isLoading ? "刷新中…" : "刷新"}
        </button>
      </div>

      {error && (
        <div className="service-page-error" role="alert">
          <p>{error}</p>
          <button className="secondary-button" disabled={isLoading} onClick={() => void loadServices()} type="button">
            重试
          </button>
        </div>
      )}

      {providerCatalog && (
        <section className="project-detail-panel service-provider-catalog" aria-labelledby="llm-route-catalog-title">
          <div>
            <p className="page-kicker">LLM 调用</p>
            <h3 id="llm-route-catalog-title">可用供应商路由</h3>
            <p>在 OpenAI 兼容请求的 model 字段中填写模型名；指定前缀时使用“前缀/模型名”，不带前缀则使用默认池。</p>
          </div>
          {providerCatalog.groups.length === 0 ? (
            <p className="service-quota-note">当前没有启用的上游连接。</p>
          ) : (
            <div className="service-route-catalog">
              {providerCatalog.groups.map((group) => (
                <div className="service-route-item" key={group.prefix ?? "default"}>
                  <code>{group.prefix ?? "默认池"}</code>
                  {group.suppliers?.length ? (
                    <span>供应商：{group.suppliers.join("、")}</span>
                  ) : (
                    <span>供应商名称暂不可用，请重启后端服务后刷新。</span>
                  )}
                  {group.prefix && <span>模型格式：{group.prefix}/模型名</span>}
                  <small>{group.connection_count} 个可用连接</small>
                </div>
              ))}
            </div>
          )}
        </section>
      )}

      {isLoading ? (
        <div className="empty-state service-page-state" aria-live="polite">正在加载服务额度…</div>
      ) : error ? null : services.length === 0 ? (
        <div className="empty-state service-page-state">
          当前没有可用服务额度。如需开通，请联系管理员。
        </div>
      ) : (
        <div className="service-quota-list">
          {services.map((service) => {
            const unitLabel = service.quota_unit === "seconds" ? "秒" : "tokens";
            const used = service.tokens_used + service.tokens_reserved;
            const usagePercent = service.monthly_token_limit > 0
              ? Math.min(100, (used / service.monthly_token_limit) * 100)
              : 0;
            const isEnabled = service.monthly_token_limit > 0;
            const isPaused = !isEnabled && (service.allocated_tokens > 0 || used > 0);

            return (
              <article className="service-quota-card" key={service.service_code}>
                <div className="service-quota-heading">
                  <div>
                    <div className="service-title-line">
                      <h3>{service.name}</h3>
                      <span className={`service-capability-badge ${isEnabled ? "service-capability-enabled" : "service-capability-disabled"}`}>
                        {isPaused ? "已暂停" : isEnabled ? "已开通" : "未开通"}
                      </span>
                    </div>
                    <p>{service.service_code} · {service.service_code === "mock-llm-v1" ? "内置 mock-chat；其他模型名请按供应商文档填写，网关会原样转发" : `模型：${service.models.join("、") || "暂无"}`}</p>
                  </div>
                  <Link className="secondary-button" to={`/services/${service.service_code === "project-context-v1" ? "context" : service.service_code === "asr-v1" ? "asr" : "llm"}/projects`}>管理项目</Link>
                </div>

                <div className="service-quota-metrics">
                  <div>
                    <span>每月总额度</span>
                    <strong>{formatTokens(service.monthly_token_limit)} <small>{unitLabel}</small></strong>
                  </div>
                  <div>
                    <span>已分配到项目</span>
                    <strong>{formatTokens(service.allocated_tokens)} <small>{unitLabel}</small></strong>
                  </div>
                  <div>
                    <span>剩余可分配</span>
                    <strong>{formatTokens(service.available_tokens)} <small>{unitLabel}</small></strong>
                  </div>
                  <div>
                    <span>本月已用 / 处理中预留</span>
                    <strong>{formatTokens(service.tokens_used)} / {formatTokens(service.tokens_reserved)} <small>{unitLabel}</small></strong>
                  </div>
                </div>

                {service.monthly_token_limit === 0 && (service.allocated_tokens > 0 || used > 0) ? (
                  <p className="service-quota-warning" role="alert">
                    此服务额度已设为 0，服务调用已暂停。项目额度分配记录仍保留；如需恢复，请联系管理员。
                  </p>
                ) : service.monthly_token_limit > 0 && service.allocated_tokens > service.monthly_token_limit ? (
                  <p className="service-quota-warning" role="alert">
                    项目额度合计高于个人月上限。项目分配记录仍保留，但所有项目的实际调用合计受个人上限限制；额度不足时网关会拒绝请求。请联系管理员或协调项目额度。
                  </p>
                ) : service.monthly_token_limit > 0 && used >= service.monthly_token_limit ? (
                  <p className="service-quota-warning" role="alert">
                    本月已用和处理中预留额度已达到或超过个人月上限，后续调用可能被网关拒绝；用量按 UTC 自然月重置。
                  </p>
                ) : null}

                <div className="service-usage-track" role="img" aria-label={`已使用与处理中预留共 ${formatTokens(used)} ${unitLabel}，月额度 ${formatTokens(service.monthly_token_limit)} ${unitLabel}`}>
                  <span style={{ width: `${usagePercent}%` }} />
                </div>
                <p className="service-quota-note">
                  {isEnabled
                    ? "额度按 UTC 自然月统计。项目调用额度从个人总额度中分配；项目消耗会同时计入这里。"
                    : isPaused
                      ? "额度为 0 时，既有项目服务分配仍保留，但不能通过个人总额度校验。"
                      : `当前尚未获得${service.name}额度。如需使用，请联系管理员调整你的月度上限。`}
                </p>
              </article>
            );
          })}
        </div>
      )}

      {user?.role === "admin" && (
        <section className="project-detail-panel admin-quota-panel" aria-labelledby="admin-quota-title">
          <div>
            <p className="page-kicker">管理员</p>
            <h3 id="admin-quota-title">用户服务额度管理</h3>
            <p className="admin-quota-description">按唯一用户名或用户 ID 查找用户，选择服务后查看并修改其每月总额度。</p>
          </div>
          <form
            className="admin-quota-lookup"
            onSubmit={(event) => {
              event.preventDefault();
              void lookupUser();
            }}
          >
            <label>
              <span>查找方式</span>
              <select
                onChange={(event) => setLookupMode(event.target.value as QuotaLookupMode)}
                value={lookupMode}
              >
                <option value="username">用户名（精确匹配）</option>
                <option value="user_id">用户 ID</option>
              </select>
            </label>
            <label className="admin-quota-search-field">
              <span>{lookupMode === "username" ? "用户名" : "用户 ID"}</span>
              <input
                autoComplete="off"
                onChange={(event) => setLookupValue(event.target.value)}
                placeholder={lookupMode === "username" ? "输入唯一用户名" : "输入用户 UUID"}
                value={lookupValue}
              />
            </label>
            <button className="primary-button" disabled={!lookupValue.trim() || isLookingUp} type="submit">
              {isLookingUp ? "查找中…" : "查找用户"}
            </button>
          </form>

          {adminError && <p className="form-error admin-quota-feedback" role="alert">{adminError}</p>}
          {adminSuccess && <p className="form-success admin-quota-feedback" role="status">{adminSuccess}</p>}

          {targetUser && targetQuota && (
            <div className="admin-quota-result">
              <div className="admin-quota-user">
                <div>
                  <strong>{targetUser.username}</strong>
                  <span>{targetUser.role === "admin" ? "管理员" : "普通用户"}</span>
                </div>
                <code>{targetUser.id}</code>
              </div>
              <label className="project-field"><span>管理服务</span><select onChange={(event) => {
                const selected = targetQuotas.find((item) => item.service_code === event.target.value);
                if (selected) {
                  setTargetQuota(selected);
                  setQuotaInput(String(selected.monthly_token_limit));
                }
              }} value={targetQuota.service_code}>{targetQuotas.map((quota) => <option key={quota.service_code} value={quota.service_code}>{quota.name}（{quota.quota_unit === "seconds" ? "秒" : "tokens"}）</option>)}</select></label>
              <div className="admin-quota-summary">
                <span>当前上限：<strong>{new Intl.NumberFormat("zh-CN").format(targetQuota.monthly_token_limit)} {targetQuota.quota_unit === "seconds" ? "秒" : "tokens"}</strong></span>
                <span>项目已分配：<strong>{new Intl.NumberFormat("zh-CN").format(targetQuota.allocated_tokens)} {targetQuota.quota_unit === "seconds" ? "秒" : "tokens"}</strong></span>
                <span>本月已用：<strong>{new Intl.NumberFormat("zh-CN").format(targetQuota.tokens_used)} {targetQuota.quota_unit === "seconds" ? "秒" : "tokens"}</strong></span>
              </div>
              {targetQuota.monthly_token_limit === 0 && (targetQuota.allocated_tokens > 0 || targetQuota.tokens_used + targetQuota.tokens_reserved > 0) ? (
                <p className="service-quota-warning" role="status">额度为 0 会暂停此用户所有项目的 LLM 调用，现有项目分配记录会保留。</p>
              ) : targetQuota.allocated_tokens > targetQuota.monthly_token_limit ? (
                <p className="service-quota-warning" role="status">当前项目分配高于用户总上限。保存新额度不会改动项目分配，但用户调用将受总上限限制，并会在个人服务页看到提醒。</p>
              ) : targetQuota.tokens_used + targetQuota.tokens_reserved >= targetQuota.monthly_token_limit && targetQuota.monthly_token_limit > 0 ? (
                <p className="service-quota-warning" role="status">本月用量及预留量已达到或超过用户总上限，后续调用可能被网关拒绝。</p>
              ) : null}
              <div className="admin-quota-edit">
                <label>
                  <span>新的 {targetQuota.name} 月额度（{targetQuota.quota_unit === "seconds" ? "秒" : "tokens"}）</span>
                  <input
                    disabled={isSavingQuota}
                    min={0}
                    onChange={(event) => setQuotaInput(event.target.value)}
                    type="number"
                    value={quotaInput}
                  />
                </label>
                <button className="primary-button" disabled={isSavingQuota} onClick={() => void saveTargetQuota()} type="button">
                  {isSavingQuota ? "保存中…" : "保存额度"}
                </button>
              </div>
              <p className="admin-quota-help">允许将额度设为 0 或调低到项目分配总额以下；项目分配保留，调用受个人总上限限制。</p>
            </div>
          )}
        </section>
      )}
    </section>
  );
}
