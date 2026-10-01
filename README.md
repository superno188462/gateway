# Agent Gateway

Agent Gateway 提供项目管理、API Key、LLM 网关、操作/技术日志和项目上下文服务。服务器 Docker 部署步骤见 [`docker.md`](docker.md)。

## 环境要求

- Python 3.11+
- uv
- Docker（仅服务器部署阶段需要；本地开发不依赖 Docker）

## PostgreSQL

后续开发和运行使用服务器 PostgreSQL。通过环境变量设置服务器连接串，不要把密码写入代码、README 或提交到 Git：

```powershell
$env:DATABASE_URL = "postgresql+asyncpg://数据库用户:数据库密码@119.45.48.180:5432/数据库名"
```

当前后端读取的应用配置包括 `DATABASE_URL`、`APP_ENV`、`LOG_LEVEL`、`LOG_FILE_PATH`、`LOG_BACKUP_COUNT`、`JWT_SECRET_KEY`、`JWT_ACCESS_TOKEN_EXPIRE_MINUTES`、`DEFAULT_LLM_MONTHLY_TOKEN_LIMIT` 和 A3 的 `API_KEY_SECRET_KEY`。日志默认写入 `backend/logs/gateway.log`，每天轮转并保留 30 个归档文件；管理员可在控制台的“技术日志”页面查看、按等级/Trace ID/事件筛选。管理员引导可同时配置 `ADMIN_USERNAME`、`ADMIN_PASSWORD`；两项都缺省时必须已经存在数据库管理员。`POSTGRES_USER`、`POSTGRES_PASSWORD` 等属于数据库容器配置，不属于应用配置，因此不放在应用 `.env.example` 中。

网关运行时使用已有 PostgreSQL 数据库。单一 `compose.yaml` 定义后端服务和前端静态文件构建导出任务；仓库不包含 Nginx 服务，服务器部署由已有 Nginx 直接托管前端并反代 API。仓库内的 PostgreSQL 仅在 `local-db` profile 下启动，服务器部署默认连接已有数据库，不会自动启动或暴露数据库。当前网关不依赖 Redis。

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

## A3 项目 API Key

在 `backend/.env` 中设置 API Key 主密钥：

```env
API_KEY_SECRET_KEY=至少32位的随机字符串
```

可在后端目录运行 `uv run python -c "import secrets; print(secrets.token_urlsafe(48))"` 生成。owner/editor 需要随时查看完整 Key，因此数据库以 Fernet 加密密文保存，并使用 HMAC 摘要验证调用。主密钥必须妥善备份并保持不变；更换后已有 Key 将无法解密或验证。未设置时后端正常启动，但 Key 管理接口返回 `503 api_key_unavailable`。

项目 owner 使用 `POST /api/admin/v1/projects/{project_id}/keys` 创建 Key。owner/editor 可通过列表随时查看完整 Key；每个 Key 默认授权整个项目，可分别用于不同客户端或运行环境。只有 owner 可通过 `DELETE /api/admin/v1/projects/{project_id}/keys/{key_id}` 撤销 Key。管理员全局 review 权限不授予 Key 访问权限。

## A4 LLM Mock 服务

新注册用户默认获得每 UTC 自然月 100,000 LLM tokens；`DEFAULT_LLM_MONTHLY_TOKEN_LIMIT` 可调整新用户默认值。管理员可在“我的服务”页按用户名或 UUID 查找用户并设置额度，对应接口包括 `GET /api/admin/v1/users/lookup?username=...`（或 `user_id=...`）、`GET /api/admin/v1/users/{user_id}/services` 和 `PUT /api/admin/v1/users/{user_id}/services/mock-llm-v1`。管理员可把上限调低到项目分配总额以下或设为 0；项目额度记录保留，网关仍按用户总上限拦截调用。个人服务页会在项目分配超出个人上限或服务暂停时提醒用户。用户可通过 `GET /api/v1/me/services` 查看自己的上限、分配和用量。

项目 owner 登录后可查询 `GET /api/admin/v1/services`，并通过 `POST /api/admin/v1/projects/{project_id}/services` 申请 `mock-llm-v1`、指定该项目的月 token 分配额。本人拥有的所有项目分配额合计不能超过个人总上限；owner 可通过 `PATCH /api/admin/v1/projects/{project_id}/services/{service_code}` 调整分配。项目用量可通过 `GET /api/admin/v1/projects/{project_id}/services` 查看。额度是 Mock 阶段的使用上限，不涉及费用或支付。

外部客户端使用项目 API Key 调用 OpenAI 风格聊天接口，项目由 Key 自动识别：

```powershell
$headers = @{ Authorization = "Bearer $env:PROJECT_API_KEY" }
$body = @{
  model = "mock-chat"
  messages = @(@{ role = "user"; content = "你好，请介绍一下这个网关" })
  max_tokens = 256
} | ConvertTo-Json -Depth 5
Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8000/v1/chat/completions -Headers $headers -ContentType "application/json" -Body $body
```

将 `stream` 设为 `true` 可返回 SSE；成功响应包含 token 用量，额度耗尽返回 429，未申请服务返回 403。Mock tokenizer 仅用于开发演示，不代表真实模型 tokenizer。网关日志记录项目、Key、模型、token、状态和请求 ID，不保存消息或回复正文。

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

生产环境按 `docker.md` 配置服务器密钥和数据库连接。公网开放前仍须配置 HTTPS 反向代理、限流、防火墙规则、备份和恢复演练。

## 文档

- [项目规格](docs/PROJECT_BRIEF.md)：产品范围、阶段目标和验收标准。
- [后端架构](docs/artifacts/backend-architecture.md)：分层、依赖方向、DI 和数据库边界。
- [UI 设计](docs/artifacts/ui-design.md)：前端页面和状态设计基准。
- [决策记录](docs/decisions.md)：已确认的产品与技术决策。
- [实施状态](docs/status.md)：当前完成情况和实际验证结果。
- [Docker 部署](docker.md)：克隆代码后的服务器配置、镜像构建、数据库迁移和启动。
- [实施步骤目录](docs/steps/README.md)：分阶段开发顺序、运行命令和 Codex 执行手册。

后续前端初始化完成后，本地开发统一使用 `npm run dev`，不使用 Docker 启动前后端。

## Git 仓库

远程仓库：`git@github.com:superno188462/gateway.git`。本地仓库只在提交前保存代码和文档，不保存 `.env`、数据库数据或真实密钥。
