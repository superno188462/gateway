# 实施状态

## 阶段计划

| 阶段 | 范围 | 验收重点 | 状态 |
|---|---|---|---|
| P0 | 规格、领域边界、OpenAPI、UI 设计 | 文档一致、范围清晰 | 已完成 |
| B0 | FastAPI、配置、PostgreSQL、Alembic、健康检查、质量工具 | 启动、迁移、测试和检查 | 已完成 |
| F0 | React 基础、路由、布局、类型安全 API Client | 能访问健康检查 | 已完成 |
| A1 | 统一登录、角色、JWT | 四种启动分支和登录流程 | 已完成 |
| A2 | 项目管理 | CRUD、项目成员权限、标签检索和分页 | 已完成；用户自测通过 |
| A3 | API Key | owner/editor 按成员创建自有 Key，私有可见，移除/降权自动撤销，日志可追溯 | 已完成 |
| A4 | LLM 网关与 OpenAI 兼容上游 | 用户级额度、项目分配、项目 Key 调用、同名直通与优先级故障切换 | 已完成；真实供应商经网关端到端验证通过（用户自测） |
| A5 | 日志与用量 | 通用请求日志、服务专属计量、数据库页码分页、汇总一致、日志保留 | 已完成 |
| A6 | 模板与记忆 | 固定目录、文件 CRUD、并发冲突 | 已完成 |
| A7 | 公网部署 | 容器构建、TLS、限流、备份、回滚和安全验收 | 已完成；服务器部署与安全验收通过（用户自测） |
| A8 | ASR 服务 | 火山豆包文件转写与实时 WebSocket、独立秒额度、事件去重和日志 | 已完成；功能测试和用户自测通过 |

## 当前状态

- A1–A8 均已完成；A2、A4、A7 的待验收项由用户自测确认通过。A8 ASR 在原阶段计划之外新增，现已纳入正式阶段并完成。
- A7 部署结构：项目提供后端镜像、前端静态构建导出任务、生产 Compose 和部署脚本；项目本身不运行 Nginx，由服务器现有 Nginx 托管静态文件并反代 API。服务器部署、TLS、限流、备份恢复与安全检查均由用户自测确认通过。
- 后续可单独规划 Embedding、TTS 等服务，不属于 A1–A8 未完成事项。
- 运行约定：后端 `uv run python main.py`；前端使用 `npm run dev`；运行时数据库使用服务器 PostgreSQL。Docker 用于服务器部署。
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
- `stream_options.include_usage` 可用；未实现的 stream option 返回 422，不静默丢弃。`stream: true` 已改为上游真实 SSE 逐帧转发：上游响应头成功后先返回下游 SSE 响应头，再实时传递数据帧；`stream: false` 保持完整 JSON 响应。上游首帧前的失败仍按连接优先级切换，首帧后失败通过 SSE error 帧结束且不重试；取消或失败会释放未使用的预留额度，已获得的上游 usage 或已输出内容会计入中断用量；成功后按上游 usage 结算，缺少 usage 时估算输出用量。
- 流式技术日志记录上游请求开始、上游响应头、首个上游数据帧、下游响应头和首个下游数据帧，均携带 Trace ID；首个上下游数据帧分别相对上游请求开始和下游响应头计时。不记录提示词、回复内容或密钥。下游继续返回 `Cache-Control: no-cache` 和 `X-Accel-Buffering: no`，实际部署的反向代理仍需关闭 SSE buffering。
- 上游 API Key 以 `LLM_PROVIDER_SECRET_KEY` 加密；第三方 URL 只允许 HTTPS，本机调试允许 loopback HTTP。
- 已对获准测试库运行 `uv run alembic upgrade head`，当前版本为 `20260929_0016 (head)`；集成测试覆盖同名透传、前缀路由、优先级顺序、密文存储和连通性测试。
- 管理员前端入口为 `/admin/llm/providers`，支持创建/修改/删除连接、前缀分组、Key 轮换、优先级设置和输入模型名测试连接；管理员列表可读回已加密保存的供应商 Key 明文。
- 供应商名称/连接名拆分后验证：`uv run pytest tests -q`（28 passed，4 skipped；跳过项要求单独配置 `TEST_DATABASE_URL`）；相关 PostgreSQL 集成测试 2 passed；ruff、格式检查、mypy、OpenAPI 契约测试、前端 typecheck/lint/build 均通过。测试库迁移版本为 `20260929_0015 (head)`。
- 管理员入口命名为“LLM API”；添加表单按一个 API 组填写供应商名称、连接名称、路由前缀和 Base URL，多行文本框每行一个 API Key，逐个保存并反馈结果。数据库允许同一连接名称下多条 API Key 记录；列表提供逐条连通性测试、编辑、停用和删除。新增 `20260929_0016` 移除连接名称唯一约束。
- 管理员列表将相同供应商名称、连接名称、前缀和 Base URL 的 API Key 收进同一组展示；管理员 API 列表返回服务端解密后的上游 Key，支持明文查看和复制。公开用户目录仍不会返回密钥。
- 同一 Base URL 和 API Key 代表同一个 API，全仓库不允许重复保存，路由前缀和显示名称不影响去重；批量粘贴在前端去重，后端创建和编辑都会查库校验，数据库用密钥派生的 HMAC 指纹加唯一约束防止并发重复；重复请求返回 HTTP 409。已有旧记录通过后端解密比较参与校验。
- PostgreSQL 集成验证确认同一组可保存多个 API Key、用户目录只显示一次供应商名称，且每条 API 可独立连通测试；对应集成测试 2 passed。测试库已升级至 `20260929_0016 (head)`。
- 后端 `uv run pytest tests -q`：23 项通过、4 项因未配置 `TEST_DATABASE_URL` 跳过；ruff、mypy 和 OpenAPI 契约测试通过。
- 前端 `npm run typecheck`、`npm run lint`、`npm run build`：全部通过。
- HTTP 集成测试使用真实 PostgreSQL 和模拟上游验证管理员权限、供应商 CRUD、密钥不泄露、最小聊天连通性请求和公开模型列表。真实方舟直连及网关经真实上游的完整调用均已由用户自测通过。
- 普通登录用户可通过 `GET /api/v1/llm/provider-catalog` 查看当前启用的默认池和路由前缀组、管理员配置的供应商显示名称及连接数；“我的服务”页展示这些供应商名称、路由前缀和 `前缀/模型名` 调用格式。目录不泄露 Base URL、优先级或 Key。
- 针对该目录新增 HTTP 集成测试，验证未登录拒绝、普通用户读取成功、管理员也可读取及返回体不含上游敏感字段；`uv run pytest tests/integration/test_llm_provider_admin_api.py -m integration -q`：1 passed。
- 更新后验证：`uv run pytest tests -q`：28 passed、4 skipped（缺少 `TEST_DATABASE_URL`）；ruff、格式检查、mypy、OpenAPI 契约测试通过；前端 typecheck、lint、build 通过。
- 供应商目录采用“供应商名称 + 上游连接名称”两层结构。新增 `20260929_0015` 为现有连接回填供应商名称；现有连接默认以原连接名作为供应商名称，管理员可编辑并将同一家供应商的连接统一名称。用户端对同一路由前缀的供应商名称去重展示。

### 验证环境说明

- 本轮迁移及 PostgreSQL 集成测试使用项目配置的获准测试数据库 `mydb`；集成测试创建随机记录并在结束时清理。
- Docker 部署验证在 A7 进行；开发环境仍可不使用 Docker。
- Docker Desktop 引擎本机未成功就绪；根据项目决策，本地开发和本轮验收不以 Docker 为前置条件。

## A5 日志与用量验证记录

- 日志页面统一称为“操作日志”，记录服务调用与项目管理操作。项目成员（含 viewer）按项目查看，管理员可跨项目筛选。项目、成员、API Key、服务额度和资源模板/记忆变更会写入操作日志，显示操作者和脱敏描述；不保存模板正文或密钥。
- `gateway_requests.event_type` 区分 `service_call` 与 `project_operation`。LLM 用量汇总只统计服务调用，管理操作不会混入调用量和 token 计量。技术日志仍是管理员专用诊断日志。
- API Key 已改为成员个人凭据：owner/editor 均可创建、查看和撤销本人创建的 Key；项目成员不能查看或撤销其他成员的 Key。成员被降为 viewer 或移出项目时，其有效 Key 在同一数据库事务中自动撤销。
- 服务调用操作日志和技术日志记录 Key 归属的成员 ID、用户名及 Key ID。历史共享 Key 在迁移时归属项目 owner，既有调用记录按该归属回填；旧历史技术日志文件不回填身份字段。

- 普通用户在项目详情进入该项目日志；后端只允许查看本人有权限的项目。管理员在全局日志页按项目、服务、状态、Request ID 和时间筛选。
- 管理员另有独立“技术日志”页面，读取后端按日轮转的运行日志文件，展示毫秒时间、等级、模块来源、事件和 Trace ID，支持按等级、Trace ID、事件内容筛选和页码分页；打开页面时读取一次，后续仅在手动刷新、提交筛选或切换页码时读取；接口仅管理员可访问。日志文件查询最多扫描尾部 5,000 行，每次只返回当前页。LLM 技术日志覆盖认证、路由、额度、上游连接尝试和服务调用结果。
- 项目调用日志 API 通过数据库 `LIMIT/OFFSET` 执行页码查询，响应明确返回 `page`、`page_size`、`total_count`、`total_pages`；前端一次只展示当前页，不将多页结果累积成一张列表。旧游标与 `limit` 请求参数仍可兼容使用。
- 新增迁移 `20260929_0023_gateway_request_page_index`，为全局日志时间倒序分页增加 `(created_at, id)` 索引；已在测试数据库升级到 `20260929_0023 (head)`。
- `GatewayRequestRecorder` 是网关服务共用的生命周期记录入口。LLM 在额度检查和上游调用前创建请求，再记录认证、额度、路由、服务调用阶段；成功结算、额度拒绝和上游失败结果都写入同一请求生命周期记录。
- 通用调用日志只包含项目/Key 归属、服务、状态、错误码与原因、耗时、Trace ID 和一段可读结果描述。LLM 模型、Token 用量和结束原因存放在独立的 `llm_request_usages` 表；Dashboard 从 LLM 专属计量数据统计 Token，不把模型或 Token 列伪装成所有服务共有的日志字段。
- 项目调用日志向用户返回成功/失败状态、可读原因、错误码、描述、耗时和 Trace ID；不返回内部审计步骤、额度核验数值、上游连接尝试或异常诊断。管理员技术日志按同一 Trace ID 提供认证、路由、额度决策（用户/项目上限、已用、预留和本次预留量）、上游连接尝试及异常细节。供应商原始错误正文不写入日志，以免混入提示词或敏感内容。
- 日志不保存 Authorization、提示词、模型回复正文或服务返回正文。LLM 日志描述可以包含用户排查所需的模型与用量摘要；结构化模型和 Token 数仍保存在 LLM 专属表中。当前 request_id 同时作为 Trace ID。
- 新增迁移 `20260929_0024_service_neutral_request_logs`，将历史 LLM 模型和 Token 数据迁至 `llm_request_usages`，并将 `gateway_requests` 改为服务无关结构；已在测试数据库升级到 head。
- 保留任务每天运行，管理员可查看最近状态并手动触发。规则保留全部最近 30 天记录，同时保证全局最新 10,000 条不会被清理；任务结果写入 `log_retention_runs`。
- 迁移 `20260929_0019` 至 `20260929_0023` 已应用，共享测试库 `mydb` 当前版本为 `20260929_0023 (head)`。
- PostgreSQL 集成测试覆盖成功、拒绝、无效 Key、审计阶段、Trace ID、脱敏摘要、项目权限隔离和管理员过滤；保留规则测试使用连接内临时表，不会清理共享数据。
- 验证：`uv run pytest -q` 为 39 passed、4 skipped；Ruff、mypy 和前端 `npm run typecheck`、`npm run lint`、`npm run build` 均通过。跳过项需要单独配置 `TEST_DATABASE_URL`。

## 网关数据库耗时优化待办

- 最近两次真实 LLM 流式调用，首个下游内容分别约 1,315 ms 和 1,324 ms；上游首块转发耗时为 0 ms，未见下游缓冲。
- 两次额度事务总耗时分别为 360 ms 和 236 ms。已拆出的查询时间包含数据库往返、数据库执行和可能的行锁等待：用户额度锁约 100/87 ms、项目订阅锁约 38/16 ms、项目用量桶约 62/39 ms、用户跨项目用量汇总约 37/19 ms。
- 请求进入至发出上游请求的整体前置阶段约 935/823 ms，但不能视为数据库耗时总和；API Key 校验、请求/LLM 用量记录初始化、模型支持检查和最终路由读取等操作尚未分别计时。
- 后续先为上述数据库操作及连接池等待补充分项计时，再依据结果决定是否合并 SQL/事务、调整连接池或采用缓存。保留用户额度行锁的并发保护，不在缺少测量时移除额度一致性控制。

## A6 模板与记忆实施记录

- 新增 `resources` 表及 Alembic 迁移 `20260930_0026`；已经把当前数据库从 `20260930_0025` 升级至 `20260930_0026 (head)`。
- 新增固定目录 `memory/{sessions,profiles,longterm}` 和 `template/{system,user,assistant}`；资源内容落数据库，不解析或拼接为服务器文件路径。活动文件在项目/目录内名称唯一，删除使用软删除，之后可重用原文件名。
- 新增项目资源目录、列表、读取、创建、更新、软删除 API。owner/editor 可写，viewer 和公开项目访问者只读；管理员全局 review 只读，若管理员本身是该项目明确 owner/editor 则按项目角色授权。所有资源读写都同时校验 project_id 与 resource_id。
- 更新/删除需要 `expected_version`；成功更新递增版本，过期版本返回 `409 version_conflict`。OpenAPI 已从 FastAPI 实际应用重新生成并提交固定错误结构。
- 集成验证：`uv run pytest -q tests/integration/test_project_resources.py` 为 1 passed；完整后端测试为 43 passed、4 skipped（均因未配置独立 `TEST_DATABASE_URL`）。`uv run ruff check app tests migrations/versions/20260930_0026_project_resources.py` 和 `uv run mypy app` 通过。
- `ruff format --check` 对 A6 文件通过；检查整个既有代码树时仍报告 4 个既有 LLM 计时实现和测试文件需要格式化，本轮没有顺带改动这些无关文件。
- UI 设计文档已补齐路由、固定目录、只读/可写角色、编辑和删除状态、并发冲突处理与接口映射；现已实现项目上下文管理页。
- A6 服务边界已澄清：模板、短期/长期记忆、用户画像组成独立的项目上下文服务，项目申请后由项目 API Key 调用；AI 推理额度只用于 LLM、Embedding、RAG、TTS、ASR 等模型服务，不用于上下文数据存取。项目订阅门禁和运行时 API 已实现。

### A6 项目上下文服务完整实现

- 服务目录注册 `project-context-v1`，项目 owner 可以在项目详情申请开通；数据服务订阅的 `monthly_token_limit` 为 `null`，上下文服务不会出现在 AI token 配额中。
- 新增 `project_session_memories`、`project_long_term_memories`、`project_user_profiles` 三张项目隔离表，迁移 `20260930_0027`、`20260930_0028` 已实际升级到数据库。
- 新增项目 API Key 运行时路由：模板读取；短期记忆读/写/删除；长期记忆分页、创建、版本更新与软删除；用户画像读取和版本化替换。项目 ID 只从 API Key 解析。原有 TTL/后台清理行为已由后续的数据保留修正取代。
- 短期消息新增 `project_session_messages` 表，按项目、外部用户、会话和数据库递增序列存储 `role/content`；支持批量追加、按顺序读取最近 1–100 条和显式清除整个会话。原有短期 key/value JSON 表和 API 保留兼容。
- 数据保留修正：短期记忆指接入项目的功能类型，不是网关保留时长。已移除读取过滤、TTL 写入和后台物理清理；迁移 `20261004_0039` 将两张会话表的旧过期时间置空并保留现存记录，今后只有显式删除 API 会清除这些数据。
- 新增 JWT 控制台项目列表与权限接口：上下文服务入口按页只列出当前用户可访问、服务已启用的运行中项目；owner/editor 可查询和编辑动态上下文，viewer 拒绝查询，管理员跨项目只读。管理员控制台查询不读取或展示项目 API Key。
- 每个通过 API Key 的上下文请求都写入 `gateway_requests`，包含服务码、Trace ID、审计结果、状态、错误码及耗时；不会写入模板、记忆或画像正文。
- 左侧“服务”父级对所有用户显示 LLM 与上下文管理子项；上下文项目页分服务展示可访问项目，项目下再进入提示词模板、短期记忆、长期记忆、用户画像四个独立路由。项目详情仍保留模板页快捷入口。
- 提示词模板通过登录态资源 API 创建/编辑/删除，运行时 API 支持按模板 ID 或分类/名称读取。Swagger UI 的模板、messages、长期记忆和画像请求模型提供完整 JSON 示例；短期新消息 API 为多条 OpenAI 风格 role/content 消息，旧 key/value 路由兼容。
- 验证：PostgreSQL `mydb` 已升级至 `20260930_0029 (head)`；`uv run pytest -m 'not integration' -q` 为 36 passed、12 deselected；上下文和资源集成测试 2 passed；`uv run ruff check app tests migrations/versions/20260930_0029_session_messages.py`、`uv run mypy app`、OpenAPI 合同测试、前端 `npm run build` 和 `npm run lint` 均通过。

## A7 Docker 部署与生产验收

- 新增 `backend/Dockerfile`、用于构建并导出前端静态文件的 `frontend/Dockerfile`、统一 `compose.yaml` 和部署脚本；镜像构建使用锁定的 Python/Node 依赖。Nginx 由服务器部署环境管理，项目不启动 Nginx 容器。
- `deploy/deploy.sh` 负责构建后端镜像、导出前端静态文件、升级 Alembic 数据库并启动后端容器。
- 新增 `docker.md`，记录 Git 克隆后配置密钥、数据库、启动、健康检查、更新和备份步骤。
- Redis 未加入部署：当前网关没有 Redis 客户端依赖或使用场景，不启动未使用的基础设施。
- 验收：用户已确认 Docker Compose、镜像构建、目标服务器启动、公网 Nginx 集成、TLS、限流、备份恢复和安全检查均通过。

## A8 ASR 服务与实时流式识别

- 新增独立 `asr-v1` 服务，用户级与项目级额度按音频秒数管理，与 LLM token 额度分开预留和结算。
- 管理员可配置火山豆包 ASR 上游、Resource ID、文件转写 URL、实时流 URL 和 API Key；密钥在服务端加密保存。业务调用方通过项目 API Key、公开模型名调用网关，不需要接触火山密钥或私有协议。
- 文件转写提供 OpenAI Audio Transcriptions 兼容接口；实时接口 `/v1/audio/stream` 接受 JSON 控制消息与 16 kHz mono s16le PCM 帧，网关负责火山 WebSocket V3 协议适配。
- 实时事件带 `utterance_id` 和可用的语句时间戳。网关按上游语句时间位置生成稳定 ID，抑制重复 final；累计 utterance 快照不会重新发送已确认的句子；相同文本在后续时间再次出现时使用新 ID。partial 只用于展示，不触发 Agent 轮次。
- 语句 final 与会话结束分离：静音判停后可在同一 WebSocket 连接上继续收发；只有调用方发送 `end` 且上游会话完成后才结算并发送 `session.completed`。
- 文档与示例：`docs/asr_websocket.md`、`docs/asr_websocket_client.py`。
- 验证：ASR 定向测试 29 项通过，Ruff 和 mypy 通过；用户确认端到端自测通过。
