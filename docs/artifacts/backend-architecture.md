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
| A3 | `api_keys`，只保存摘要、前缀和末四位 |
| A4 | `gateway_requests`、`usage_records`、可空的 `cost_records` |
| A5 | 查询索引、审计事件和日志保留任务状态 |
| A6 | `resources` 及乐观并发版本 |

## 配置契约

- `DATABASE_URL` 必须是 `postgresql+asyncpg://` 连接串。
- `APP_ENV` 取值为 `development`、`test` 或 `production`。
- `LOG_LEVEL` 取标准日志级别。
- 配置对象由 `AppContainer.settings` 注册为单例；业务代码通过依赖注入取得，不直接调用 `Settings()`。
- 数据库引擎和基础设施服务由 `AppContainer` 注册为单例，并在应用关闭时统一释放。
- B0 不校验管理员变量；管理员引导逻辑属于 A1，避免基础设施阶段提前创建业务表。

## 健康检查

- `GET /health/live`：只检查应用事件循环可响应，返回 200。
- `GET /health/ready`：通过注入的 `ReadinessProbe` 执行数据库 `SELECT 1`。成功返回 200；失败返回 503 和稳定错误码，不返回异常文本或连接信息。

数据库实现是 Adapter；单元测试使用 Fake Probe，集成测试使用真实 PostgreSQL。
