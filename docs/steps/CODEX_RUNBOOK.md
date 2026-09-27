# Codex 分阶段执行手册

## 文档职责

把 `../PROJECT_BRIEF.md` 当作产品范围和验收标准，把 `../artifacts/backend-architecture.md` 当作后端分层与 DI 约束，把 `../artifacts/ui-design.md` 当作页面设计基准，把 `../decisions.md` 当作决策记录，把 `../status.md` 当作当前进度。API 契约以 `../../contracts/openapi.yaml` 为准。

本手册只描述如何执行阶段，不重复完整产品规格。每次只执行一个阶段，阶段验收通过后再开始下一阶段。

## 通用执行规则

每个阶段都按以下顺序工作：

1. 阅读上述事实来源和当前代码，确认本阶段范围。
2. 先更新或补充 API 契约、用户流程和验收场景。
3. 实现后端端点、Application Service、Domain Port 和 Infrastructure Adapter。
4. 先运行后端单元、合同和集成测试，失败时修复后再继续。
5. 后端通过后实现对应前端页面和状态。
6. 用浏览器或 HTTP 客户端完成最小端到端流程。
7. 更新 `../status.md`，记录改动、验证命令、结果、未验证依赖和下一步。

实现约束：

- 依赖方向保持 `HTTP API → Application Service → Domain Port ← Infrastructure Adapter`。
- 配置和跨请求单例服务必须注册到 `AppContainer`，路由使用 FastAPI `Depends` 获取，不自行创建。
- 运行时数据库使用服务器 PostgreSQL；本机不依赖 Docker。
- 后端使用 `uv`、`pyproject.toml` 和 `uv.lock`；后端启动命令为 `uv run python main.py`。
- 前端完成后使用 `npm run dev`；前端通过类型安全 API Client 调用后端。
- 第三方服务先使用 Mock Adapter，不得自行选择真实 Provider。
- 不提交 `.env`、密码、API Key、连接串或其他密钥。
- 不实现当前阶段之外的业务，不创建没有契约和验收标准的空壳端点。
- 保留用户已有文件和未相关的改动。

## 当前启动指令：A1 管理员认证

P0 和 B0 已完成。本轮只执行 A1，不实现项目管理、API Key、Mock 网关、Dashboard、模板、前端业务页面或 Docker 部署。

```text
请阅读：

- ../PROJECT_BRIEF.md
- ../artifacts/backend-architecture.md
- ../decisions.md
- ../status.md
- ../../contracts/openapi.yaml
- 当前 backend 代码

执行 A1：管理员认证。先检查当前代码和 DI 容器，不要重建 B0。

后端范围：
1. 设计并更新 users 数据模型和 Alembic 迁移。
2. 实现唯一管理员约束。
3. 实现 ADMIN_USERNAME 和 ADMIN_PASSWORD 的可选引导规则：两项同时配置时创建或更新数据库管理员；两项同时缺省时使用数据库已有管理员；数据库无管理员且环境变量缺省时启动失败；只配置一项时启动失败。
4. 密码只保存安全哈希，环境变量明文不得进入数据库、日志或 API 响应。
5. 实现登录、退出或令牌失效所需的后端接口和 JWT 认证依赖。
6. 将配置、密码哈希服务、JWT 服务、用户仓储和管理员引导服务注册到 AppContainer，通过 Depends 注入。
7. 先完成后端测试：迁移、四种配置分支、登录成功、密码错误、令牌失效和唯一管理员约束。
8. 更新 OpenAPI、README、../status.md 和必要的决策记录。

先只完成后端。不要初始化前端，不要实现项目和 API Key。

完成后运行 uv sync、alembic migration、pytest、ruff、mypy，并报告实际结果。不要自动进入 A2。
```

## A1 后端验收后

```text
A1 后端接口和测试已验收。现在只实现 A1 对应的登录页面和认证状态管理。

要求：
- 先阅读 ../artifacts/ui-design.md，遵循现有页面结构和状态设计。
- 通过共享 API Client 调用登录接口，不自行拼接未定义的 URL 或字段。
- 覆盖加载、成功、错误、重复提交、刷新恢复、退出和令牌失效状态。
- 运行前端 lint、类型检查、单元测试、构建，并使用浏览器完成登录和退出流程。
- 更新 ../status.md；不要进入 A2。
```

## 后续阶段模板

```text
请阅读 ../PROJECT_BRIEF.md、../artifacts/backend-architecture.md、../decisions.md、../status.md、../../contracts/openapi.yaml 和当前代码。

本轮只执行 {阶段名称}。先明确本阶段的用户流程、API 契约、数据库变更和验收场景，再实现。

要求：
1. 跨请求服务和配置注册到 AppContainer，通过 Depends 注入。
2. 契约修改同步更新 OpenAPI、Pydantic 模型、前端类型、Mock 和合同测试。
3. 后端先实现并验证，前端随后实现并完成端到端验收。
4. 覆盖成功、空数据、加载、可恢复错误、不可恢复错误、取消、重复提交和权限失败。
5. 失败必须修复并重新运行受影响的验证。
6. 更新 ../status.md，列出实际改动、验证命令、通过/失败/未验证项目和风险。

本阶段完成后停止，不要自动进入下一阶段。
```

## 阶段顺序

```text
P0 规格与设计       已完成
  ↓
B0 后端基础         已完成
  ↓
F0 前端基础
  ↓
A1 管理员认证       当前
  ↓
A2 项目管理
  ↓
A3 API Key
  ↓
A4 Mock 网关
  ↓
A5 日志与用量
  ↓
A6 模板与记忆
  ↓
A7 公网部署
```

## 固定验收指令

```text
请基于当前 diff、../status.md 和实际运行结果做阶段验收。

逐项对照 ../PROJECT_BRIEF.md 的当前阶段完成条件，列出：
- 通过及证据；
- 失败及原因；
- 未验证及缺少的外部条件。

重点检查：契约一致性、DI 注册、项目隔离、密钥脱敏、数据库迁移、错误结构和关键用户流程。当前阶段的问题直接修复并重新验证；不要开始下一阶段。
```
