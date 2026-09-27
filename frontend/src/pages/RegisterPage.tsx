import { FormEvent, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { ApiClientError, apiClient } from "../api/client";

export function RegisterPage() {
  const navigate = useNavigate();
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [confirmPassword, setConfirmPassword] = useState("");
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (isSubmitting) return;
    setError(null);
    if (password !== confirmPassword) {
      setError("两次输入的密码不一致");
      return;
    }
    setIsSubmitting(true);
    try {
      await apiClient.register({ username: username.trim(), password });
      navigate("/login", { replace: true, state: { registered: true } });
    } catch (reason) {
      setError(
        reason instanceof ApiClientError
          ? reason.body?.message ?? "注册失败，请检查输入。"
          : "无法连接后端，请确认服务已经启动。",
      );
    } finally {
      setIsSubmitting(false);
    }
  }

  return (
    <main className="login-page">
      <section className="login-panel" aria-labelledby="register-title">
        <div className="login-brand">
          <span className="brand-mark">AG</span>
          <span>Agent Gateway</span>
        </div>
        <p className="page-kicker">创建账户</p>
        <h1 id="register-title">注册普通用户</h1>
        <p className="login-description">注册后需要被加入项目，才能访问对应项目资源。</p>
        <form className="login-form" onSubmit={handleSubmit}>
          <label>
            用户名
            <input
              autoComplete="username"
              disabled={isSubmitting}
              minLength={3}
              onChange={(event) => setUsername(event.target.value)}
              required
              value={username}
            />
          </label>
          <label>
            密码
            <input
              autoComplete="new-password"
              disabled={isSubmitting}
              minLength={8}
              onChange={(event) => setPassword(event.target.value)}
              required
              type="password"
              value={password}
            />
          </label>
          <label>
            确认密码
            <input
              autoComplete="new-password"
              disabled={isSubmitting}
              onChange={(event) => setConfirmPassword(event.target.value)}
              required
              type="password"
              value={confirmPassword}
            />
          </label>
          {error && <p className="form-error" role="alert">{error}</p>}
          <button className="login-button" disabled={isSubmitting} type="submit">
            {isSubmitting ? "注册中…" : "注册"}
          </button>
        </form>
        <p className="login-note">
          已有账户？<Link to="/login">返回登录</Link>
        </p>
      </section>
    </main>
  );
}
