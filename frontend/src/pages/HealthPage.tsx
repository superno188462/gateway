import { useCallback, useEffect, useState } from "react";
import { apiClient, ApiClientError, type HealthResponse } from "../api/client";

type CheckState = {
  status: "idle" | "loading" | "ok" | "error";
  data?: HealthResponse;
  error?: string;
};

const initialCheck: CheckState = { status: "idle" };

function HealthCard({ label, check }: { label: string; check: CheckState }) {
  const isLoading = check.status === "loading";
  const isOk = check.status === "ok";
  return (
    <article className={`health-card health-card-${check.status}`}>
      <div className="health-card-heading">
        <span className={`health-icon ${isOk ? "health-icon-ok" : ""}`} aria-hidden="true">
          {isOk ? "✓" : "•"}
        </span>
        <div>
          <h3>{label}</h3>
          <span className="health-endpoint">{label === "进程存活" ? "/health/live" : "/health/ready"}</span>
        </div>
      </div>
      <p className="health-value">
        {isLoading ? "检查中…" : isOk ? "正常" : check.status === "error" ? "不可用" : "未检查"}
      </p>
      {check.error && <p className="health-error">{check.error}</p>}
    </article>
  );
}

export function HealthPage() {
  const [live, setLive] = useState<CheckState>(initialCheck);
  const [ready, setReady] = useState<CheckState>(initialCheck);
  const [lastChecked, setLastChecked] = useState<Date | null>(null);

  const checkHealth = useCallback(async () => {
    setLive({ status: "loading" });
    setReady({ status: "loading" });
    const [liveResult, readyResult] = await Promise.allSettled([
      apiClient.getLiveness(),
      apiClient.getReadiness(),
    ]);
    setLive(
      liveResult.status === "fulfilled"
        ? { status: "ok", data: liveResult.value }
        : { status: "error", error: formatError(liveResult.reason) },
    );
    setReady(
      readyResult.status === "fulfilled"
        ? { status: "ok", data: readyResult.value }
        : { status: "error", error: formatError(readyResult.reason) },
    );
    setLastChecked(new Date());
  }, []);

  useEffect(() => {
    void checkHealth();
  }, [checkHealth]);

  return (
    <section className="page-content" aria-labelledby="health-title">
      <div className="intro-row">
        <div>
          <p className="page-kicker">基础设施</p>
          <h2 id="health-title">服务健康检查</h2>
          <p className="page-description">
            查看后端进程和 PostgreSQL 数据库是否可以正常接收请求。
          </p>
        </div>
        <button className="primary-button" onClick={() => void checkHealth()} type="button">
          刷新状态
        </button>
      </div>
      <div className="health-grid">
        <HealthCard check={live} label="进程存活" />
        <HealthCard check={ready} label="数据库就绪" />
      </div>
      <div className="verification-note">
        <span className="note-icon" aria-hidden="true">i</span>
        <div>
          <strong>F0 验收范围</strong>
          <p>当前只验证前端基础工程和 B0 健康接口，业务模块将在后续阶段逐步开放。</p>
          {lastChecked && <small>最近检查：{lastChecked.toLocaleTimeString()}</small>}
        </div>
      </div>
    </section>
  );
}

function formatError(reason: unknown): string {
  if (reason instanceof ApiClientError) {
    return reason.body?.message ?? `HTTP ${reason.status}`;
  }
  return "无法连接后端，请确认后端已启动。";
}
