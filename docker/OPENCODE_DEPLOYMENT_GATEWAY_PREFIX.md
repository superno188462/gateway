# OpenCode 部署任务：将网关部署在 `/gateway` 路径下

本文供服务器上的 OpenCode 执行后续部署改造时参考。Agent Gateway 项目自身不运行 Nginx；服务器已有的 Nginx 属于外部部署环境，由服务器 OpenCode 检查现状后自行选择安全的配置方式。目标是让 Agent Gateway 与同域名下的其他应用共存，不占用它们的 `/api/` 或 `/v1/` 路径。

## 部署目标

统一使用外部前缀：

- 控制台页面：`https://<域名>/gateway/`
- 控制台管理 API：`https://<域名>/gateway/api/...`
- 项目服务 API（LLM、上下文等）：`https://<域名>/gateway/v1/...`

后端容器内部路由暂时保持现有路径：控制台 API 仍为 `/api/...`，项目服务 API 仍为 `/v1/...`。由外部 Nginx 转发时移除 `/gateway` 前缀。不要把后端 FastAPI 路由整体改成 `/gateway/api` 或 `/gateway/v1`，也不要将这两个路径转给其他应用。

## 实施前检查

1. 确认仓库中的 `compose.yaml`、前后端 Dockerfile、Vite 配置、React Router、前端 API 客户端及 `APP_ROOT_PATH` 当前值；项目本身不提供或启动 Nginx。
2. 记录 Nginx 当前容器/服务名、配置文件挂载位置、网关 Compose 项目目录、Docker 网络和现有对外端口。保留当前 TLS 证书、其他站点与 location 配置。
3. 备份将要修改的 Nginx 配置文件。使用仓库实际 Compose 文件和环境变量，不要另造一套数据库或 Redis 容器；当前网关没有 Redis 依赖。
4. 检查 `.env`、密钥、数据库凭据和其他项目配置，不要将其写入仓库或输出到日志。

## 前端要求

仓库代码已配置生产构建使用 `/gateway/` 子路径，并保留本地开发根路径行为。部署时先检查这些设置与服务器选定的前缀一致；如需变更，改代码后重新构建，不要只改浏览器侧 Nginx 路由：

- Vite 生产资源基路径为 `/gateway/`，确保 JS、CSS、图片等资源从 `/gateway/assets/...` 加载。
- React Router 的 basename 为 `/gateway`，确保直接打开或刷新 `/gateway/<页面路由>` 可用。
- 控制台 API 请求使用 `/gateway/api/...`。
- 直接发起请求的上下文页面及其他绕过公共 API 客户端的代码使用 `/gateway/v1/...`。
- 检查全仓前端代码中的绝对路径、登录跳转、下载/复制的 API 地址及 OpenAPI 地址，确保浏览器侧没有意外请求根路径 `/api/` 或 `/v1/`。
- 尽量通过统一配置或 URL helper 管理前缀，不要零散硬编码。开发环境可继续通过 Vite proxy 访问本地后端。

## Nginx 转发要求

外层 Nginx 按以下规则分流，并在转发时删除外部 `/gateway` 前缀：

| 外部请求 | 内部上游 | 上游收到的路径 |
|---|---|---|
| `/gateway/` 和 `/gateway/<前端路由>` | 由现有服务器 Nginx 从其挂载的 `deploy/www/` 静态目录提供 | 静态文件已按 `/gateway/` 构建；SPA 路由回退到 `index.html` |
| `/gateway/api/...` | 后端容器 `agent-gateway-api:8000` | `/api/...` |
| `/gateway/v1/...` | 后端容器 `agent-gateway-api:8000` | `/v1/...` |
| `/gateway/health/...`、`/gateway/docs`、`/gateway/redoc`、`/gateway/openapi.json` | 后端容器 `agent-gateway-api:8000` | 对应的 `/health/...`、`/docs`、`/redoc`、`/openapi.json` |

服务器 Nginx 的实际配置完全由 OpenCode 自行决定，不要求采用仓库内的配置片段。它需要检查现有 Nginx 部署方式、静态文件根目录、容器 bind mount、Docker 网络、TLS 和其他站点配置，再完成上述路径映射。关键是外部 API 前缀被正确转换为后端已有的 `/api/...` 或 `/v1/...` 路由；`/gateway/api/health/...` 需转成后端 `/health/...`，或另行提供等价健康检查路径；SSE 流式 API 不得被代理缓冲。不得替换或覆盖其他站点配置。

## Docker 与网络要求

- 后端运行容器和前端构建导出任务由仓库的单一 `compose.yaml` 管理；项目不包含 Nginx 容器或 Nginx 配置。
- 部署脚本把前端构建文件导出到服务器仓库目录 `deploy/www/`。现有 Nginx 需要能读取这个目录，并加入共享 Docker 网络通过 `agent-gateway-api` 访问后端。
- 后端端口继续只在 Docker 网络内开放；除非用户明确要求，不要把 `8000` 直接发布到公网。
- 保留现有 PostgreSQL 配置和持久化数据；不要运行会删除 volume 或重建数据库的命令。
- 后端 SSE/流式响应必须关闭 Nginx buffering。

## 验证清单

部署前先执行前端 lint/build、后端相关测试、`docker compose config`、`docker compose build`。部署后逐项验证：

1. `/gateway/` 返回控制台页面，静态资源均从 `/gateway/` 加载。
2. 登录、刷新和直接打开一个嵌套路由均成功。
3. 浏览器 Network 面板中的控制台请求走 `/gateway/api/...`，LLM/上下文请求走 `/gateway/v1/...`；没有请求到根路径 `/api/`、`/v1/`。
4. 健康检查成功；`/gateway/docs` 可打开且 Swagger 请求使用 `/gateway` 前缀。
5. 使用项目 API Key 调用 `/gateway/v1/...`，验证鉴权、错误响应及流式 SSE 首帧能正常到达。
6. 同域名的其他应用仍能使用自己的 `/api/`、`/v1/`，没有被网关 location 截获。
7. 用 `nginx -t` 检查配置后再 reload；检查网关容器健康状态和日志。保留部署前备份及回滚步骤。

## 完成时向用户汇报

列出修改文件和 Nginx 配置位置，说明外部到内部的路径映射、实际测试结果、未验证项目及回滚方法。不要声称已部署成功，除非服务器上的容器、Nginx 和 HTTPS 请求均经过验证。
