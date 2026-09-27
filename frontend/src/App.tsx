import { NavLink, Navigate, Route, Routes } from "react-router-dom";
import { AuthProvider } from "./auth/AuthProvider";
import { useAuth } from "./auth/useAuth";
import { HealthPage } from "./pages/HealthPage";
import { LoginPage } from "./pages/LoginPage";
import { RegisterPage } from "./pages/RegisterPage";

const navigation = [
  { label: "运行状态", to: "/" },
  { label: "项目", to: "/projects", disabled: true },
  { label: "调用日志", to: "/logs", disabled: true },
];

function AppShell() {
  const { user, isLoading, logout } = useAuth();
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
            <h1>运行状态</h1>
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
