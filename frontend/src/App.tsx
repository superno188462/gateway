import { NavLink, Navigate, Route, Routes, useLocation } from "react-router-dom";
import { AuthProvider } from "./auth/AuthProvider";
import { useAuth } from "./auth/useAuth";
import { HealthPage } from "./pages/HealthPage";
import { LoginPage } from "./pages/LoginPage";
import { RegisterPage } from "./pages/RegisterPage";
import { ProjectsPage } from "./pages/ProjectsPage";
import { ProjectDetailPage } from "./pages/ProjectDetailPage";
import { ProjectCreatePage } from "./pages/ProjectCreatePage";
import { ProjectApiKeysPage } from "./pages/ProjectApiKeysPage";
import { MyServicesPage } from "./pages/MyServicesPage";
import { LlmProvidersPage } from "./pages/LlmProvidersPage";

const navigation = [
  { label: "运行状态", to: "/" },
  { label: "项目", to: "/projects" },
  { label: "我的服务", to: "/account/services" },
  { label: "调用日志", to: "/logs", disabled: true },
];

function AppShell() {
  const { user, isLoading, logout } = useAuth();
  const location = useLocation();
  const pageTitle = location.pathname === "/account/services"
    ? "我的服务"
    : location.pathname === "/admin/llm/providers"
      ? "LLM API"
      : location.pathname === "/projects/new"
    ? "创建项目"
    : location.pathname.endsWith("/keys")
      ? "API Key"
      : location.pathname.startsWith("/projects/")
      ? "项目详情"
      : location.pathname === "/projects"
        ? "项目"
        : "运行状态";
  if (isLoading) return <div className="app-loading">正在恢复登录状态…</div>;

  return (
    <div className="app-shell">
      <aside className="sidebar" aria-label="主导航">
        <div className="brand">
          <span className="brand-mark">AG</span>
          <div>
            <strong>Agent Gateway</strong>
            <span>控制台</span>
          </div>
        </div>
        <nav className="nav-list">
          {navigation.map((item) =>
            item.disabled ? (
              <span className="nav-item nav-item-disabled" key={item.to} aria-disabled="true">
                <span>{item.label}</span>
                <small>即将开放</small>
              </span>
            ) : (
              <NavLink
                className={({ isActive }) => `nav-item${isActive ? " nav-item-active" : ""}`}
                key={item.to}
                to={item.to}
              >
                {item.label}
              </NavLink>
            ),
          )}
          {user?.role === "admin" && (
            <NavLink
              className={({ isActive }) => `nav-item${isActive ? " nav-item-active" : ""}`}
              to="/admin/llm/providers"
            >
              LLM API
            </NavLink>
          )}
        </nav>
        <div className="sidebar-footer">
          <span className="status-dot" aria-hidden="true" />
          B0 基础环境
        </div>
      </aside>
      <main className="main-content">
        <header className="topbar">
          <div>
            <span className="eyebrow">平台概览</span>
            <h1>{pageTitle}</h1>
          </div>
          <div className="account-area">
            <span className="user-badge">{user?.username} · {user?.role}</span>
            <button className="logout-button" onClick={() => void logout()} type="button">
              退出
            </button>
          </div>
        </header>
        <Routes>
          <Route element={<HealthPage />} path="/" />
          <Route element={<ProjectsPage />} path="/projects" />
          <Route element={<ProjectCreatePage />} path="/projects/new" />
          <Route element={<ProjectDetailPage />} path="/projects/:projectId" />
          <Route element={<ProjectApiKeysPage />} path="/projects/:projectId/keys" />
          <Route element={<MyServicesPage />} path="/account/services" />
          <Route element={user?.role === "admin" ? <LlmProvidersPage /> : <Navigate replace to="/" />} path="/admin/llm/providers" />
          <Route element={<Navigate replace to="/" />} path="*" />
        </Routes>
      </main>
    </div>
  );
}

export default function App() {
  return (
    <AuthProvider>
      <Routes>
        <Route element={<LoginPage />} path="/login" />
        <Route element={<RegisterPage />} path="/register" />
        <Route element={<ProtectedApp />} path="/*" />
      </Routes>
    </AuthProvider>
  );
}

function ProtectedApp() {
  const { token, isLoading } = useAuth();
  if (isLoading) return <div className="app-loading">正在恢复登录状态…</div>;
  if (!token) return <Navigate replace to="/login" />;
  return <AppShell />;
}
