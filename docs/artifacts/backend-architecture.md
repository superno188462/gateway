# 后端架构与第一阶段模型

## 边界

B0 只建立可运行、可迁移、可测试的后端基础，不提前创建管理员、项目、API Key 或调用日志表。业务表由对应阶段在契约和用例确定后通过独立 Alembic 迁移加入。

## 依赖方向

```text
app/api          HTTP 协议、校验、响应映射
    ↓
app/application  用例编排与事务边界（B0 暂无业务用例）
    ↓
app/domain       领域实体与 Port（B0 仅定义健康探针 Protocol）
    ↑
app/infrastructure  PostgreSQL、Provider 等 Adapter
```

`app/bootstrap.py` 只负责创建 FastAPI 和 `AppContainer`。`app/container.py` 是组合根中的 DI 注册表：配置、数据库引擎和就绪探针在应用生命周期内各创建一次，服务通过 FastAPI `Depends` 获取。API 路由不得创建数据库引擎、读取环境变量或自行实例化跨请求服务。后续 A1 起的密码哈希服务、JWT 服务、仓储和应用服务都注册到同一容器。

## 目录

```text
backend/
├── app/
│   ├── api/                 # HTTP 路由
│   ├── domain/              # 与框架无关的 Port
│   ├── infrastructure/db/   # SQLAlchemy 引擎和数据库探针
│   ├── container.py          # DI 注册表和 Depends 适配器
│   ├── bootstrap.py         # 组合根
│   ├── config.py            # 类型化配置
│   └── main.py              # ASGI 入口
├── migrations/              # Alembic 迁移
├── tests/unit/              # 无真实外部依赖
├── tests/integration/       # PostgreSQL 验证
└── pyproject.toml
```

## 首阶段数据模型

B0 不创建业务实体。数据库只产生 Alembic 自己的 `alembic_version` 表，用于证明空库可升级并为后续迁移建立基线。

后续阶段的模型归属：

| 阶段 | 模型 |
|---|---|
| A1 | `users`，包含唯一管理员约束与密码哈希 |
| A2 | `projects`（含 `public/private` 可见性）、`project_members`、`project_tags`；公开访问按规则授予只读权限，管理员默认 review 全部项目，owner 管理项目与最多 5 个自定义标签 |
| A3 | `api_keys`，保存 HMAC-SHA256 摘要与 Fernet 加密密文；一个项目可有多把项目级 Key |
| A4 | `user_service_quotas`、`project_service_subscriptions`、`service_usage_buckets`、通用 `gateway_requests`；LLM 专属用量单独写入 `llm_request_usages` |
| A5 | 通用请求日志查询、审计阶段和日志保留任务状态；模型与 Token 等服务专属信息不得成为通用日志列 |
| A6 | `resources`、`project_session_messages`、`project_session_memories`、`project_long_term_memories`、`project_user_profiles` 及乐观并发版本 |
| A8 | `asr_provider_configs`；复用服务额度、项目订阅和通用请求日志，ASR 按音频秒数单独计量 |

### A6 资源模块

固定分类由 `app.resources.domain` 声明：`memory/sessions`、`memory/profiles`、`memory/longterm`、`template/system`、`template/user`、`template/assistant`。资源以 `project_id + resource_type + category + name` 标识当前有效文件，内容存储在 PostgreSQL；名称不能包含路径分隔符，资源分类不能由请求动态创建。删除通过 `deleted_at` 软删除，允许后续用相同名称新建。

`app.resources.application` 编排项目访问授权、事务和并发校验，SQLAlchemy 数据访问留在 `app.resources.infrastructure`。管理员凭全局身份可以跨项目读取但不能写入；项目 owner/editor 可以在其所属项目创建、修改、软删除。管理员若是该项目的明确 owner/editor 成员，按成员角色授权。viewer 和公开项目访客只读。每次读写都同时限定项目 ID 和资源 ID。更新、删除必须携带 `expected_version`；版本匹配时递增，过期版本返回 `409 version_conflict`，不覆盖并发修改。

项目上下文服务以项目 API Key 确定运行时项目。短期标准消息在 `project_session_messages` 按项目、外部用户、会话和数据库递增序列存储；原有 `project_session_memories` key/value JSON 接口继续兼容。长期记忆是文本条目及可选 tags/metadata，用户画像是一份项目自定义 JSON。控制台项目列表使用 `project-context-v1` 订阅和项目可见范围分页；动态数据查询使用 JWT 管理端 API，owner/editor 可读写，管理员只读，viewer 被拒绝。控制台管理查询不要求公开或读取 API Key。

## 配置契约

- `DATABASE_URL` 必须是 `postgresql+asyncpg://` 连接串。
- `APP_ENV` 取值为 `development`、`test` 或 `production`。
- `LOG_LEVEL` 取标准日志级别。
- `LOG_FILE_PATH` 指定 UTF-8 技术日志路径，默认为 `logs/gateway.log`；`LOG_BACKUP_COUNT` 默认保留 30 个按日轮转的归档文件。技术日志只允许管理员通过系统日志接口读取。
- `API_KEY_SECRET_KEY` 至少 32 个字符，用于派生 API Key 摘要和加密密钥。未配置时应用可启动，但 Key 管理和验证返回 503；该值必须稳定保存，轮换会使已有 Key 无法解密和验证。
- `gateway_requests` 固定保存跨服务字段：项目与 Key 归属、服务代码、状态、错误、耗时、Trace ID 和简短结果描述。LLM 模型、Token 用量与结束原因写入 `llm_request_usages`，由 LLM 模块维护。
- 配置对象由 `AppContainer.settings` 注册为单例；业务代码通过依赖注入取得，不直接调用 `Settings()`。
- 数据库引擎和基础设施服务由 `AppContainer` 注册为单例，并在应用关闭时统一释放。
- B0 不校验管理员变量；管理员引导逻辑属于 A1，避免基础设施阶段提前创建业务表。

## 健康检查

- `GET /health/live`：只检查应用事件循环可响应，返回 200。
- `GET /health/ready`：通过注入的 `ReadinessProbe` 执行数据库 `SELECT 1`。成功返回 200；失败返回 503 和稳定错误码，不返回异常文本或连接信息。

数据库实现是 Adapter；单元测试使用 Fake Probe，集成测试使用真实 PostgreSQL。

## A4 服务模块结构

每种具体能力在 `app/services/` 下保持同级，拥有自己的接口、应用用例、HTTP 路由和 Provider 适配器。跨服务的服务目录、项目申请、额度和用量管理位于 `app/service_management/`；目录项契约位于 `app/domain/service_catalog.py`。组合根通过 `AppContainer` 将各服务目录项注册给通用服务管理，并注册服务模块的网关实现。

```text
app/
├── services/
│   ├── llm/                 # LLM API、网关用例、领域契约、Provider
│   ├── project_context/     # 项目模板、短期/长期记忆、用户画像 API
│   │   └── providers/mock.py
│   ├── asr/                 # A8 ASR HTTP、WebSocket、额度生命周期和火山 Provider
│   ├── tts/                 # 后续新增，与 llm 平级
│   └── embedding/            # 后续新增，与 llm 平级
├── service_management/       # 跨服务目录、申请、额度与用量管理
├── domain/service_catalog.py # 服务模块与通用管理共享的目录契约
└── container.py              # 服务目录与实现的组合根 / DI 注册
```

- `services/llm/domain.py` 定义 `LlmProvider` Port 和厂商无关的消息/结果类型；`services/llm/providers/` 提供确定性 Mock 和支持 OpenAI 兼容协议的可配置上游 Provider。
- 管理员可通过 `services/llm/configuration.py` 的 API 配置多个 OpenAI 兼容上游连接；连接可配置共享路由前缀，`volc/model` 只会调用 `volc` 组并向上游传 `model`，未带前缀时进入全局连接池。组内和全局连接都按优先级从小到大排序。供应商密钥以 `LLM_PROVIDER_SECRET_KEY` 派生的 Fernet 密钥加密后写入数据库；管理员专用列表接口会解密返回，普通用户接口不返回密钥。
- 同一 LLM API 组可为共享供应商名、连接名、路由前缀和 Base URL 添加多个 API Key；每个密钥作为独立连接凭据保存、测试和启停。
- `services/llm/providers/openai_compatible.py` 按优先级依次调用启用连接，在网络错误、400/401/403/404/408/409/422/429 或 5xx 时尝试其他连接；第三方 URL 必须为 HTTPS，本机 HTTP 仅供开发调试。
- 管理员可以请求兼容的 `/models` 测试上游连通性，数据库仅保存测试时间、成功状态及安全摘要，不保存供应商原始响应。
- 登录用户通过 `GET /api/v1/llm/provider-catalog` 查看启用的默认池和路由前缀组、去重的供应商显示名及连接数量；公开目录不含连接名、上游 URL、密钥或优先级。

### A8 ASR 模块

`services/asr/` 提供 OpenAI 风格文件转写和统一实时 WebSocket 接口。管理员上游密钥加密保存于 `asr_provider_configs`；文件转写与实时流各自适配火山协议。网关使用通用项目 API Key、额度及请求审计能力，ASR 用音频秒数独立预留与结算。实时 Provider 将火山帧转换为统一事件，按上游 utterance 时间戳生成稳定 ID 并去重 final；音频和识别正文不写入请求日志。
- `services/llm/application.py` 校验服务开通、预留月额度、调用 Provider、结算用量和记录请求；`services/llm/api.py` 提供 OpenAI 风格聊天 API。
- 网关保留并透传 Chat Completions 标准字段及未声明的 JSON 扩展字段；只重写公开 `model` 到供应商模型名，并替换鉴权。供应商拒绝参数时返回可诊断的安全错误；响应保留供应商兼容字段，但请求正文不入库。
- `service_management/application.py` 通过容器注入的服务目录管理项目申请、额度和用量；`service_management/api.py` 暴露现有服务目录、订阅、申请和额度 API。
- 服务目录中的 AI 推理服务（LLM、Embedding、RAG、TTS、ASR）受服务专属额度计量；LLM 使用 token，A8 ASR 使用音频秒数，后续服务须明确各自单位。项目上下文服务是独立的数据存取服务，通过项目订阅门禁，但不扣 AI 推理额度；容量和频率限制由该服务自己的配置处理。
- `services/project_context` 通过目录码 `project-context-v1` 注册为独立数据服务。`ProjectContextService` 以项目 API Key 解析的 project_id 为根边界，先检查项目订阅，再按用户和会话键访问记录。其运行时路由通过 `GatewayRequestRecorder` 记录服务结果元数据，不把数据正文传给记录器。
- 现有 HTTP 路径和服务目录行为保持兼容。添加新服务时，在 `services/<name>/` 实现模块，并在组合根注册对应目录项；服务管理代码不依赖某个具体 Provider。
- 调用方使用 `Authorization: Bearer <项目 API Key>`。API Key 服务解析项目 ID，客户端不传项目 ID；认证后的项目必须已有对应服务订阅。
- `service_usage_buckets` 按项目、服务和 UTC 月初聚合 token；通过数据库行锁序列化同项目同服务的额度预留，避免并发请求同时突破月度上限。
- `gateway_requests` 保存请求追踪和 token 元数据，不关联外键以便项目/Key 后续删除时保留运营日志；不含 prompt、messages、completion 或 response 正文。
- 新用户默认获得配置的 LLM 月度上限（默认 100,000 tokens）；管理员可按用户调整。个人服务接口返回上限、项目分配合计、可分配余额和用户全部项目的当月用量。
- 项目 owner 申请或调整项目服务时，事务锁定用户额度并校验所有项目分配额合计；管理员可把用户总上限调低到已有项目分配合计以下，网关仍同时校验项目服务额度和用户跨项目总上限。个人服务页和项目详情在超配时提醒用户；管理员只能 review 项目，但拥有账户级额度配置权限。
