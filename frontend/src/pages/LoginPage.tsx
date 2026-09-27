import { FormEvent, useState } from "react";
import { useNavigate } from "react-router-dom";
import { ApiClientError } from "../api/client";
import { useAuth } from "../auth/useAuth";

export function LoginPage() {
  const navigate = useNavigate();
  const { login } = useAuth();
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (isSubmitting) return;
    setError(null);
    setIsSubmitting(true);
    try {
      await login({ username: username.trim(), password });
      navigate("/", { replace: true });
    } catch (reason) {
      setError(
        reason instanceof ApiClientError
          ? reason.body?.message ?? "用户名或密码错误"
          : "无法连接后端，请确认服务已经启动。",
      );
    } finally {
      setIsSubmitting(false);
    }
  }

  return (
    <main className="login-page">
      <section className="login-panel" aria-labelledby="login-title">
        <div className="login-brand">
          <span className="brand-mark">AG</span>
          <span>Agent Gateway</span>
        </div>
        <p className="page-kicker">安全访问</p>
        <h1 id="login-title">登录控制台</h1>
        <p className="login-description">使用已配置的账号访问网关管理能力。</p>
        <form className="login-form" onSubmit={handleSubmit}>
          <label>
            用户名
            <input
              autoComplete="username"
              disabled={isSubmitting}
              onChange={(event) => setUsername(event.target.value)}
              required
              value={username}
            />
          </label>
          <label>
            密码
            <input
              autoComplete="current-password"
              disabled={isSubmitting}
              onChange={(event) => setPassword(event.target.value)}
              required
              type="password"
              value={password}
            />
          </label>
          {error && <p className="form-error" role="alert">{error}</p>}
          <button className="login-button" disabled={isSubmitting} type="submit">
            {isSubmitting ? "登录中…" : "登录"}
          </button>
        </form>
        <p className="login-note">管理员账号由服务端环境变量或数据库管理员记录维护。</p>
      </section>
    </main>
  );
}
