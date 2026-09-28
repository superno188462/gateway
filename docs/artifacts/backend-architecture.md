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
| A4 | `user_service_quotas`、`project_service_subscriptions`、`service_usage_buckets`、`gateway_requests`；日志不保存提示词或回复正文 |
| A5 | 查询索引、审计事件和日志保留任务状态 |
| A6 | `resources` 及乐观并发版本 |

## 配置契约

- `DATABASE_URL` 必须是 `postgresql+asyncpg://` 连接串。
- `APP_ENV` 取值为 `development`、`test` 或 `production`。
- `LOG_LEVEL` 取标准日志级别。
- `API_KEY_SECRET_KEY` 至少 32 个字符，用于派生 API Key 摘要和加密密钥。未配置时应用可启动，但 Key 管理和验证返回 503；该值必须稳定保存，轮换会使已有 Key 无法解密和验证。
- 配置对象由 `AppContainer.settings` 注册为单例；业务代码通过依赖注入取得，不直接调用 `Settings()`。
- 数据库引擎和基础设施服务由 `AppContainer` 注册为单例，并在应用关闭时统一释放。
- B0 不校验管理员变量；管理员引导逻辑属于 A1，避免基础设施阶段提前创建业务表。

## 健康检查

- `GET /health/live`：只检查应用事件循环可响应，返回 200。
- `GET /health/ready`：通过注入的 `ReadinessProbe` 执行数据库 `SELECT 1`。成功返回 200；失败返回 503 和稳定错误码，不返回异常文本或连接信息。

数据库实现是 Adapter；单元测试使用 Fake Probe，集成测试使用真实 PostgreSQL。

## A4 LLM Mock 垂直切片

- `app/domain/llm.py` 定义 `LlmProvider` Port 和与厂商无关的消息/结果类型；`infrastructure/mock_llm.py` 实现确定性 Mock，未来真实 Provider 只替换 DI 装配。
- `application/llm_services.py` 管理项目服务目录、owner 申请与额度升级；`application/llm_gateway.py` 校验服务开通、预留月额度、调用 Provider、结算用量和记录请求。
- `api/llm_services.py` 提供服务目录、项目订阅/用量、申请和升级 API；`api/llm_gateway.py` 提供面向 Agent 的 OpenAI 风格聊天 API。
- 调用方使用 `Authorization: Bearer <项目 API Key>`。API Key 服务解析项目 ID，客户端不传项目 ID；认证后的项目必须已有对应服务订阅。
- `service_usage_buckets` 按项目、服务和 UTC 月初聚合 token；通过数据库行锁序列化同项目同服务的额度预留，避免并发请求同时突破月度上限。
- `gateway_requests` 保存请求追踪和 token 元数据，不关联外键以便项目/Key 后续删除时保留运营日志；不含 prompt、messages、completion 或 response 正文。
- 新用户默认获得配置的 LLM 月度上限（默认 100,000 tokens）；管理员可按用户调整。个人服务接口返回上限、项目分配合计、可分配余额和用户全部项目的当月用量。
- 项目 owner 申请或调整项目服务时，事务锁定用户额度并校验所有项目分配额合计；管理员可把用户总上限调低到已有项目分配合计以下，网关仍同时校验项目服务额度和用户跨项目总上限。个人服务页和项目详情在超配时提醒用户；管理员只能 review 项目，但拥有账户级额度配置权限。
