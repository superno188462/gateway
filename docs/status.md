# 实施状态

## 阶段计划

| 阶段 | 范围 | 验收重点 | 状态 |
|---|---|---|---|
| P0 | 规格、领域边界、OpenAPI、UI 设计 | 文档一致、范围清晰 | 已完成 |
| B0 | FastAPI、配置、PostgreSQL、Alembic、健康检查、质量工具 | 启动、迁移、测试和检查 | 已完成 |
| F0 | React 基础、路由、布局、类型安全 API Client | 能访问健康检查 | 已完成 |
| A1 | 统一登录、角色、JWT | 四种启动分支和登录流程 | 已完成 |
| A2 | 项目管理 | CRUD、项目成员权限、标签检索和分页 | 功能已完成；隔离 PostgreSQL 集成验收待执行 |
| A3 | API Key | owner/editor 可查看明文、加密存储、撤销 | 已完成 |
| A4 | LLM 网关与 OpenAI 兼容上游 | 用户级额度、项目分配、项目 Key 调用、同名直通与优先级故障切换 | 功能已完成；真实供应商经网关端到端验证待执行 |
| A5 | 日志与用量 | 筛选、分页、汇总一致 | 未开始 |
| A6 | 模板与记忆 | 固定目录、文件 CRUD、并发冲突 | 未开始 |
| A7 | 公网部署 | TLS、限流、备份、回滚和安全验收 | 未开始 |

## 当前阶段

- 当前：A4 LLM 网关、管理员供应商配置页面、OpenAI 兼容上游连接池和用户级额度均已实现；上游模型名默认同名直通。
- 本轮不包含：ASR/TTS/Embedding/RAG/记忆/画像、A5 日志页面和公网部署。
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

## A3 后端验证记录

- 新增 `api_keys` 表和 `20260928_0007_api_keys` Alembic 迁移；外键关联项目并级联清理，摘要和前缀唯一，状态仅允许 active/revoked。
- 一个项目可创建多把项目级 Key；每把 Key 默认授权整个项目。项目 owner/editor 可查看脱敏 Key 元数据；只有 owner 可创建和撤销，管理员全局 review 权限不包含 Key 访问权。
- Key 使用 `agw_` 前缀和密码学安全随机值生成；数据库保存 HMAC-SHA256 摘要及 Fernet 密文，运行时绝不将明文写入数据库。owner/editor 列表可随时查看完整 Key；API 不返回摘要。
- 新增项目 Key 的创建、列表、撤销接口及内部校验服务。校验会拒绝无效、撤销、过期 Key，并更新 `last_used_at`；撤销操作幂等。
- `API_KEY_SECRET_KEY` 可选以保持现有部署可启动；未配置时 Key 管理接口返回 503，不使用默认值签发 Key。至少 32 个字符，需稳定备份；轮换会使已签发 Key 无法解密或验证。
- 已在 `.env` 配置的 `119.45.48.180:5432/mydb` 执行 `uv run alembic upgrade head`，版本先到 `20260928_0007`；本次新增 `20260928_0008`，增加密文列并撤销无法恢复的旧 Key。
- `uv run pytest tests/integration/test_api_keys.py -m integration -q`：通过；覆盖创建、owner/editor 随时可查看完整 Key、数据库只保存密文和摘要、仅 owner 可操作、有效 Key 校验、撤销及撤销后拒绝。测试仅清理自身随机测试项目和用户。
- `uv run pytest -m 'not integration'`：11 项通过；`uv run ruff check --fix .`、`uv run ruff format .`、`uv run mypy app tests main.py`：通过；OpenAPI YAML 可解析，合同路径测试通过。
- 未执行其他既有集成测试，因为 A2 测试会清理共享数据库中的用户和项目；不对共享 `mydb` 运行此类破坏性用例。
- 前端新增 `/projects/:projectId/keys`：owner/editor 从项目详情进入并查看完整 Key；owner 可创建和撤销，editor 只读；viewer 和管理员跨项目 review 不显示入口。
- Key 列表支持复制完整 Key；创建支持永不过期、30/90/365 天，撤销需要二次确认。服务端使用 Fernet 加密密文持久化，使页面刷新后仍可按权限查看。
- `npm run typecheck`、`npm run lint`、`npm run build`：通过。
- 当前开发环境 `backend/.env` 已生成 `API_KEY_SECRET_KEY` 并启用本机功能；该文件被 Git 忽略。部署到服务器时，必须将同一个值安全配置到服务端环境，否则无法解密该数据库中已有 Key。

## A4 LLM Mock 后端验证记录

- 新增 `20260928_0009_llm_mock_gateway` 和 `20260928_0010_user_service_quotas` 迁移，创建项目服务订阅、用户服务月额度、项目/服务/UTC 月用量桶和无正文网关请求表；0010 为已有账号发放默认额度，并保留既有项目额度分配。
- 新用户默认获得每 UTC 自然月 100,000 个 LLM Mock tokens，可由 `DEFAULT_LLM_MONTHLY_TOKEN_LIMIT` 调整；管理员可按用户设置或撤销 LLM 月度额度。新用户注册在用户与额度同一事务中完成。
- 用户可查询个人 LLM 服务能力、月总上限、跨项目分配额、剩余可分配量及本月总消耗；项目 owner 可申请服务并指定项目月额度，或调整已有项目额度。正常申请/调整时项目分配合计不能超过用户上限；管理员下调用户上限时允许形成超配，调用仍受个人总额度限制，页面会提醒用户。项目额度不得低于当月已用与预留量。
- 管理员对项目仍只有全局只读 review 权限；管理员可配置用户账户级服务额度，但不能替项目 owner 调整项目服务分配。
- 新增 OpenAI 风格 `POST /v1/chat/completions`，Bearer 项目 API Key 自动解析项目，调用端不传 project ID；支持 JSON 与 SSE、`stream_options.include_usage`。
- 网关同时对用户跨项目总额度和项目自身额度核验；按 prompt 估算和 `max_tokens` 预留额度，调用成功后记 Provider 用量并释放多余预留。无用户能力/项目服务返回 403，模型不存在 404，任一额度不足返回 429，Provider 错误 502。响应和日志提供请求 ID。
- `gateway_requests` 仅记录项目/Key/服务/模型/状态/token/延迟/错误码/请求 ID，不保存提示词或模型回复正文；Mock tokenizer 是粗估，替换真实 Provider 后应使用 Provider 返回用量。
- 已在获准使用的测试数据库执行 `uv run alembic upgrade head`，目标版本为 `20260928_0010 (head)`。
- PostgreSQL 集成流程覆盖默认/单用户额度、用户名/UUID 管理员查找、项目分配超额后下调用户上限、归零暂停调用、重新授额、JSON、SSE、错误 Key、请求记录关联及不保存正文；测试只清理自身随机测试记录。
- `uv run pytest -m 'not integration' -q`：13 passed，6 deselected。
- `uv run pytest tests/integration/test_api_keys.py tests/integration/test_llm_mock_gateway.py -m integration -q`：2 passed；只清理本次生成的随机测试数据。
- `uv run ruff check .`、`uv run ruff format --check .`、`uv run mypy app tests main.py`、`uv lock --check`：全部通过；OpenAPI 合同测试包含在单元测试中。
- 已在获准使用的测试数据库执行 `uv run alembic upgrade head`；`uv run alembic current` 为 `20260928_0010 (head)`。
- 未运行会删除共享数据库项目/用户的旧 A2 集成测试。
- 前端 `/account/services` 展示个人月上限、项目分配总额、可分配额度和跨项目月用量；项目详情为 owner 提供服务申请、额度调整和本项目使用量。
- 管理员可在“我的服务”页按精确用户名或 UUID 查找用户、查看额度汇总并修改 LLM 月额度；允许额度低于项目分配总和或设为 0，用户侧会显示超配/暂停提醒。查找与目标用户额度查询接口仅管理员可访问。
- `npm run typecheck`、`npm run lint`、`npm run build`：全部通过。

## A4 LLM 网关与 OpenAI 兼容供应商连接池

- 新增迁移 `20260929_0013_llm_provider_priority` 和 `20260929_0014_llm_provider_route_prefix`：供应商连接支持优先级和可选路由前缀；旧模型映射表暂时保留历史数据，但不再参与调用或管理员 API。
- 管理员 API 支持连接增删改、优先级和启用/停用；连通性测试请求提交临时模型名，向该连接发送最小 `/chat/completions` 请求；API 只返回 Key 是否已配置和测试摘要。
- LLM 网关默认把调用方模型名原样传给全局上游池；`prefix/model` 只路由到配置了该前缀的连接组，并去掉前缀后转发模型名。组内从优先级最高的启用连接开始；网络错误、400/401/403/404/408/409/422/429 或 5xx 时按顺序切换。内置 `mock-chat` 行为不变。
- Chat Completions 网关保留并透传标准采样参数、tools、response_format 和未声明扩展字段；供应商兼容响应字段（含工具调用结果）原样保留，公开响应中的 model 仍使用网关模型名。上游以 400/422 拒绝参数时返回清晰的参数错误，不记正文。
- `stream_options.include_usage` 可用；未实现的 stream option 返回 422，不静默丢弃。当前 `stream: true` 会先等上游完整响应，再由网关编码 SSE，尚未实现上游实时 SSE 的端到端透传。
- 上游 API Key 以 `LLM_PROVIDER_SECRET_KEY` 加密；第三方 URL 只允许 HTTPS，本机调试允许 loopback HTTP。
- 已对获准测试库运行 `uv run alembic upgrade head`，当前版本为 `20260929_0014 (head)`；集成测试覆盖同名透传、前缀路由、优先级顺序、密文存储和连通性测试。
- 管理员前端入口为 `/admin/llm/providers`，支持创建/修改/删除连接、前缀分组、Key 轮换、优先级设置和输入模型名测试连接；密钥不会从服务端读回。
- 本轮验证：`uv run pytest tests -q`（28 passed，4 skipped；跳过项要求单独配置 `TEST_DATABASE_URL`）、`uv run ruff check app tests`、`uv run mypy app`、`npm run build`、`npm run lint` 均通过。
- 后端 `uv run pytest tests -q`：23 项通过、4 项因未配置 `TEST_DATABASE_URL` 跳过；ruff、mypy 和 OpenAPI 契约测试通过。
- 前端 `npm run typecheck`、`npm run lint`、`npm run build`：全部通过。
- HTTP 集成测试使用真实 PostgreSQL 和模拟上游验证管理员权限、供应商 CRUD、密钥不泄露、最小聊天连通性请求和公开模型列表。真实方舟直连已由用户使用 curl 验证；网关经真实上游的完整调用仍待验证。
- 普通登录用户可通过 `GET /api/v1/llm/provider-catalog` 查看当前启用的默认池和路由前缀组、管理员配置的供应商显示名称及连接数；“我的服务”页展示这些供应商名称、路由前缀和 `前缀/模型名` 调用格式。目录不泄露 Base URL、优先级或 Key。
- 针对该目录新增 HTTP 集成测试，验证未登录拒绝、普通用户读取成功、管理员也可读取及返回体不含上游敏感字段；`uv run pytest tests/integration/test_llm_provider_admin_api.py -m integration -q`：1 passed。
- 更新后验证：`uv run pytest tests -q`：28 passed、4 skipped（缺少 `TEST_DATABASE_URL`）；ruff、格式检查、mypy、OpenAPI 契约测试通过；前端 typecheck、lint、build 通过。

### 验证环境说明

- 本轮迁移及 PostgreSQL 集成测试使用项目配置的获准测试数据库 `mydb`；集成测试创建随机记录并在结束时清理。
- Docker 部署验证仍留到 A7；本轮无需 Docker。
- Docker Desktop 引擎本机未成功就绪；根据项目决策，本地开发和本轮验收不以 Docker 为前置条件。

## 下一步

进入 A5 日志检索与 Dashboard。
