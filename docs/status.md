# 实施状态

## 阶段计划

| 阶段 | 范围 | 验收重点 | 状态 |
|---|---|---|---|
| P0 | 规格、领域边界、OpenAPI、UI 设计 | 文档一致、范围清晰 | 已完成 |
| B0 | FastAPI、配置、PostgreSQL、Alembic、健康检查、质量工具 | 启动、迁移、测试和检查 | 已完成 |
| F0 | React 基础、路由、布局、类型安全 API Client | 能访问健康检查 | 已完成 |
| A1 | 统一登录、角色、JWT | 四种启动分支和登录流程 | 已完成 |
| A2 | 项目管理 | CRUD、项目成员权限、标签检索和分页 | 功能已完成；隔离 PostgreSQL 集成验收待执行 |
| A3 | API Key | 单次明文、哈希存储、撤销 | 未开始 |
| A4 | Mock 网关 | Key 调用、token 和请求记录 | 未开始 |
| A5 | 日志与用量 | 筛选、分页、汇总一致 | 未开始 |
| A6 | 模板与记忆 | 固定目录、文件 CRUD、并发冲突 | 未开始 |
| A7 | 公网部署 | TLS、限流、备份、回滚和安全验收 | 未开始 |

## 当前阶段

- 当前：A2 功能已实现；待在隔离 PostgreSQL 上执行 0005/0006 迁移并完成集成验收，再进入 A3。
- 本轮不包含：API Key、Mock 模型接口、日志用量、模板记忆和公网部署。
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

## A2 验证记录

- 新增 `projects`、`project_members` 数据模型及 `20260928_0004_projects` Alembic 迁移。
- 新增 `project_tags` 标签关系表及 `20260928_0006_project_tags` 迁移；项目最多 5 个自定义标签，每个最多 20 字符，服务端统一去空格、转小写和去重。
- 所有已登录用户均可创建项目，创建者自动成为 `owner`；管理员可 review 所有项目。
- 项目支持 `public` / `private`：普通用户可查看所有公开项目和显式加入的私有项目；公开访问授予只读 review，不产生每用户成员行。
- 私有项目由成员关系控制查看；只有项目 owner 可以更新项目或管理成员，管理员全局角色只提供 review。
- 项目成员支持 `owner`、`editor`、`viewer` 角色；不能通过成员接口新增、修改或移除 owner。
- 新增项目 API：项目 CRUD、成员查询、添加成员、移除成员。
- 新增前端 `/projects` 项目列表和 `/projects/:projectId` 独立详情页：所有登录用户可创建 public/private 项目；owner 在详情页编辑项目和成员权限；管理员和公开项目的所有用户只读 review。
- 项目列表支持名称/描述关键词、标签、状态筛选及服务端分页；标签候选仅来自当前用户可见项目，owner 可在创建和项目设置中维护标签。
- 仅项目 owner 可永久删除项目；API 返回 204，删除前端二次确认，标签和成员关系随项目清理。真实 PostgreSQL 删除集成测试待隔离数据库环境执行。
- 项目列表和详情页依据 `owner_id` 显示“我创建的”或“他人项目”，让用户能区分自己的项目与公开 review 项目。
- 成员 API 的添加请求支持 `user_id` 或唯一 `username` 二选一；成员列表和添加响应均返回用户 ID 与用户名，支持修改非 owner 成员角色，项目描述支持通过 PATCH 置空。
- 迁移 `20260928_0005_project_visibility` 为既有项目设置默认 `private`；`0005`、`0006` 尚未执行，本轮不连接生产数据库。
- `uv run pytest -m "not integration"`：10 项通过；PostgreSQL 集成测试未执行。
- `uv run ruff check .`、`uv run ruff format --check .`、`uv run mypy app tests main.py`：通过。
- `npm run typecheck`、`npm run lint`、`npm run build`：通过。

### 验证环境说明

- 本轮之前使用过项目忽略目录中的独立 PostgreSQL 18.6 临时实例，监听 `127.0.0.1:55432`；迁移和测试完成后已停止。当前 Docker Desktop 引擎未启动，无法重新创建隔离实例。
- 没有修改本机已有的 PostgreSQL 5432 服务，也没有使用服务器数据库凭据。
- 目标生产版本 PostgreSQL 16 的 Docker 部署与迁移验证留到 A7；B0 已验证 SQLAlchemy、Alembic 和健康探针在真实 PostgreSQL 上工作。
- Docker Desktop 引擎本机未成功就绪；根据项目决策，本地开发和本轮验收不以 Docker 为前置条件。

## 下一步

在隔离 PostgreSQL 上依次执行 0005、0006 迁移，验证公开/私有项目、标签增删/筛选/分页及管理员只读权限；完成验收后再进入 A3。
