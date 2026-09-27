# Agent Gateway

当前仓库完成 P0 设计、B0 后端基础和 F0 前端基础。业务功能和管理员认证尚未实现。

## 环境要求

- Python 3.11+
- uv
- Docker（仅服务器部署阶段需要；本地开发不依赖 Docker）

## PostgreSQL

后续开发和运行使用服务器 PostgreSQL。通过环境变量设置服务器连接串，不要把密码写入代码、README 或提交到 Git：

```powershell
$env:DATABASE_URL = "postgresql+asyncpg://数据库用户:数据库密码@119.45.48.180:5432/数据库名"
```

当前后端读取的应用配置包括 `DATABASE_URL`、`APP_ENV`、`LOG_LEVEL`、`JWT_SECRET_KEY` 和 `JWT_ACCESS_TOKEN_EXPIRE_MINUTES`。A1 首次启动还可以同时配置 `ADMIN_USERNAME`、`ADMIN_PASSWORD` 引导唯一管理员；两项都缺省时必须已经存在数据库管理员。`POSTGRES_USER`、`POSTGRES_PASSWORD` 等属于 Docker 数据库容器配置，不属于应用配置，因此不放在当前 `.env.example` 中。

本轮 B0 已使用隔离的本地 PostgreSQL 临时实例完成验证，但不会作为项目运行时数据库。`compose.yaml` 当前只包含 PostgreSQL 服务骨架；完整的前后端服务器 Docker 部署脚本将在 A7 阶段补齐后才可用于生产部署。

## 安装后端依赖并迁移

```powershell
cd backend
uv sync --all-groups
uv run alembic upgrade head
```

## 启动后端

```powershell
cd backend
uv run python main.py
```

健康检查：

```powershell
Invoke-RestMethod http://127.0.0.1:8000/health/live
Invoke-RestMethod http://127.0.0.1:8000/health/ready
```

`/health/live` 只确认进程可响应；`/health/ready` 会执行 `SELECT 1` 验证数据库连接。响应不会暴露数据库连接串。

## A1 管理员认证

首次启动前，在 `backend/.env` 中同时配置：

```env
ADMIN_USERNAME=admin
ADMIN_PASSWORD=请替换为至少 8 位的强密码
JWT_SECRET_KEY=请替换为至少 32 位的随机字符串
```

应用会在单个事务中创建或更新唯一管理员。管理员成功写入数据库后，可以移除前两项管理员配置，后续启动会继续使用数据库中的管理员；`JWT_SECRET_KEY` 必须持续保留。所有用户统一通过 `POST /api/v1/auth/login` 登录，JWT 中携带 `role`，管理员权限由受保护业务接口判断。

普通用户可以通过 `POST /api/v1/auth/register` 注册。注册只会创建 `role=user` 账号，不能创建管理员；注册成功后使用统一登录接口登录。

## 验证

```powershell
cd backend
uv run ruff check .
uv run ruff format --check .
uv run mypy app tests
uv run pytest
```

使用服务器 PostgreSQL 运行集成测试：

```powershell
cd backend
$env:TEST_DATABASE_URL = $env:DATABASE_URL
uv run --env-file ../.env pytest -m integration
```

根目录原有的 `test.py` 和根 `pyproject.toml` 是独立的 PostgreSQL 连通性测试，B0 未修改其用途。

## 配置

复制 `.env.example` 为 `backend/.env` 作为本地配置，不要提交真实 `.env`。应用默认读取启动目录下的 `backend/.env`；如果将配置放在仓库根目录，则使用 `uv run --env-file ../.env ...` 显式加载。

生产环境必须通过服务器密钥配置替换示例数据库凭据，通过 HTTPS/反向代理暴露服务，并在 A7 阶段完成限流和公网安全配置。

## 文档

- [项目规格](docs/PROJECT_BRIEF.md)：产品范围、阶段目标和验收标准。
- [后端架构](docs/artifacts/backend-architecture.md)：分层、依赖方向、DI 和数据库边界。
- [UI 设计](docs/artifacts/ui-design.md)：前端页面和状态设计基准。
- [决策记录](docs/decisions.md)：已确认的产品与技术决策。
- [实施状态](docs/status.md)：当前完成情况和实际验证结果。
- [实施步骤目录](docs/steps/README.md)：分阶段开发顺序、运行命令和 Codex 执行手册。

后续前端初始化完成后，本地开发统一使用 `npm run dev`，不使用 Docker 启动前后端。

## Git 仓库

远程仓库：`git@github.com:superno188462/gateway.git`。本地仓库只在提交前保存代码和文档，不保存 `.env`、数据库数据或真实密钥。
