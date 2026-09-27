# 实施状态

## 阶段计划

| 阶段 | 范围 | 验收重点 | 状态 |
|---|---|---|---|
| P0 | 规格、领域边界、OpenAPI、UI 设计 | 文档一致、范围清晰 | 已完成 |
| B0 | FastAPI、配置、PostgreSQL、Alembic、健康检查、质量工具 | 启动、迁移、测试和检查 | 已完成 |
| F0 | React 基础、路由、布局、类型安全 API Client | 能访问健康检查 | 已完成 |
| A1 | 统一登录、角色、JWT | 四种启动分支和登录流程 | 已完成 |
| A2 | 项目管理 | CRUD、刷新持久化和隔离 | 未开始 |
| A3 | API Key | 单次明文、哈希存储、撤销 | 未开始 |
| A4 | Mock 网关 | Key 调用、token 和请求记录 | 未开始 |
| A5 | 日志与用量 | 筛选、分页、汇总一致 | 未开始 |
| A6 | 模板与记忆 | 固定目录、文件 CRUD、并发冲突 | 未开始 |
| A7 | 公网部署 | TLS、限流、备份、回滚和安全验收 | 未开始 |

## 当前阶段

- 当前：P0、B0、F0 与 A1 已完成，注册切片已加入，等待进入 A2 项目管理。
- 本轮不包含：管理员认证、项目业务、API Key、Mock 模型接口、前端业务页面和公网部署。
- 运行约定：后端 `uv run python main.py`；前端在 F0 后使用 `npm run dev`；运行时数据库使用服务器 PostgreSQL。Docker 仅用于后续服务器部署。
- DI 约定：`AppContainer` 注册配置、数据库引擎和基础设施服务单例；路由通过 FastAPI `Depends` 获取，不自行创建服务。

## P0 交付

- `docs/artifacts/backend-architecture.md`：后端边界、目录、依赖方向和首阶段数据模型。
- `contracts/openapi.yaml`：B0 健康检查契约。
- `docs/artifacts/ui-design.md`：后续前端的信息架构、页面状态和 API 映射；未实现 UI。
- `docs/decisions.md`：产品和架构决策。

## B0 验证记录

- `uv sync --all-groups`：通过，生成 `backend/uv.lock` 并安装锁定依赖。
- `uv run alembic upgrade head`：通过，从空库升级到 `20260927_0001`。
- `uv run alembic current`：通过，当前版本为 `20260927_0001 (head)`。
- `uv run pytest`：通过，9 项测试全部通过（`TEST_DATABASE_URL` 指向隔离的临时 PostgreSQL 18.6）；包含 DI 单例、真实 PostgreSQL 就绪探针和 OpenAPI 合同一致性测试。
- `uv run pytest -m integration`：通过，1 项真实 PostgreSQL 就绪检查通过。
- `uv run ruff check .`：通过。
- `uv run ruff format --check .`：通过，19 个文件格式正确。
- `uv run mypy app tests main.py`：通过，17 个源文件无类型错误。
- `uv run python main.py`：实际启动成功。
- `GET /health/live`：实际 HTTP 请求返回 `200 {"status":"ok"}`。
- `GET /health/ready`：实际连接数据库并返回 `200 {"status":"ok"}`。
- `GET /openapi.json`：实际返回健康检查两条路径。
- DI 改造后再次用临时 PostgreSQL 启动 `uv run python main.py`：`/health/live` 和 `/health/ready` 均返回 `ok`。

## F0 验证记录

- `npm install`：通过，生成 `frontend/package-lock.json`。
- `npm run typecheck`：通过。
- `npm run lint`：通过。
- `npm run build`：通过，生成 Vite 生产构建产物。
- `npm run dev`：实际启动成功，访问 `http://127.0.0.1:5173/` 返回 200。
- Vite 代理验证：`/api/health/live` 和 `/api/health/ready` 均成功转发到后端并返回 `{"status":"ok"}`。
- F0 已完成 React + TypeScript 工程、路由骨架、控制台布局、健康检查页面和类型安全 API Client；未实现 A1 登录页及后续业务页面。

## A1 后端验证记录

- 新增 `users` 和通用 `auth_sessions` 数据模型及 `20260927_0002_admin_auth`、`20260927_0003_role_based_auth` Alembic 迁移。
- `ADMIN_USERNAME` 与 `ADMIN_PASSWORD` 必须同时配置或同时缺省；缺少管理员且未配置引导信息时启动失败。
- 管理员密码使用 Argon2 哈希保存；JWT 使用配置的 `JWT_SECRET_KEY` 签发，令牌会话支持主动撤销。
- 新增统一认证接口 `POST /api/v1/auth/login`、`POST /api/v1/auth/logout` 和 `GET /api/v1/auth/me`；JWT 携带角色，管理员权限由授权依赖判断。
- 集成测试覆盖管理员创建、已有管理员续用、登录、`/me`、退出和令牌撤销；使用隔离 PostgreSQL 18.6 实例验证。
- `uv run pytest`：13 项全部通过（包含 3 项真实 PostgreSQL 集成测试）。
- `uv run ruff check .`、`uv run ruff format --check .`、`uv run mypy app tests main.py`、`uv lock --check`：全部通过。
- 使用临时数据库启动 Uvicorn 后，实际登录、获取当前管理员和退出请求分别返回 `200`、`200`、`204`。

## A1 前端验证记录

- 登录页独立路由：`/login`。
- 登录成功后保存 JWT，调用 `/api/v1/auth/me` 恢复当前用户和角色。
- 页面覆盖登录中、登录失败、后端不可用、重复提交、退出和令牌失效后的回登录页状态。
- `npm run typecheck`：通过。
- `npm run lint`：通过。
- `npm run build`：通过。
- 项目业务页面仍未实现，下一阶段进入 A2。

## 注册功能验证记录

- 新增 `POST /api/v1/auth/register`，只创建 `role=user` 普通用户，不允许通过注册成为管理员。
- 用户名长度为 3–100 个字符，密码长度为 8–256 个字符；用户名重复返回 `409 username_taken`。
- 注册密码使用与管理员相同的 Argon2 哈希服务保存。
- 前端新增 `/register` 页面，包含确认密码、重复提交保护、错误提示和注册成功后返回登录。
- PostgreSQL 集成测试覆盖注册成功、重复用户名冲突和普通用户登录。

### 验证环境说明

- 本轮使用项目忽略目录中的独立 PostgreSQL 18.6 临时实例，监听 `127.0.0.1:55432`；迁移和测试完成后已停止。
- 没有修改本机已有的 PostgreSQL 5432 服务，也没有使用服务器数据库凭据。
- 目标生产版本 PostgreSQL 16 的 Docker 部署与迁移验证留到 A7；B0 已验证 SQLAlchemy、Alembic 和健康探针在真实 PostgreSQL 上工作。
- Docker Desktop 引擎本机未成功就绪；根据项目决策，本地开发和本轮验收不以 Docker 为前置条件。

## 下一步

B0、F0 与 A1 已完成。下一步进入 A2，实现项目、项目成员关系和项目级权限。
