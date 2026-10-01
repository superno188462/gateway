# OpenCode 部署任务：将网关部署在 `/gateway` 路径下

本文供服务器上的 OpenCode 执行后续部署改造时参考。目标是让 Agent Gateway 与同域名下的其他应用共存，不占用它们的 `/api/` 或 `/v1/` 路径。

## 部署目标

统一使用外部前缀：

- 控制台页面：`https://<域名>/gateway/`
- 控制台管理 API：`https://<域名>/gateway/api/...`
- 项目服务 API（LLM、上下文等）：`https://<域名>/gateway/v1/...`

后端容器内部路由暂时保持现有路径：控制台 API 仍为 `/api/...`，项目服务 API 仍为 `/v1/...`。由外部 Nginx 转发时移除 `/gateway` 前缀。不要把后端 FastAPI 路由整体改成 `/gateway/api` 或 `/gateway/v1`，也不要将这两个路径转给其他应用。

## 实施前检查

1. 确认仓库中的 `compose.yaml`、前后端 Dockerfile、Vite 配置、React Router、前端 API 客户端及 `deploy/nginx/default.conf` 当前内容；不要覆盖服务器已有的 Nginx 配置。
2. 记录 Nginx 当前容器/服务名、配置文件挂载位置、网关 Compose 项目目录、Docker 网络和现有对外端口。保留当前 TLS 证书、其他站点与 location 配置。
3. 备份将要修改的 Nginx 配置文件。使用仓库实际 Compose 文件和环境变量，不要另造一套数据库或 Redis 容器；当前网关没有 Redis 依赖。
4. 检查 `.env`、密钥、数据库凭据和其他项目配置，不要将其写入仓库或输出到日志。

## 前端要求

需要让生产构建可在 `/gateway/` 子路径下工作，并保留开发环境原有行为：

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
| `/gateway/` 和 `/gateway/<前端路由>` | 前端容器 `agent-gateway-web:80` | 原路径去掉 `/gateway`，SPA 回退到 `index.html` |
| `/gateway/api/...` | 后端容器 `agent-gateway-api:8000` | `/api/...` |
| `/gateway/v1/...` | 后端容器 `agent-gateway-api:8000` | `/v1/...` |
| `/gateway/health/...`、`/gateway/docs`、`/gateway/redoc`、`/gateway/openapi.json` | 后端容器 `agent-gateway-api:8000` | 对应的 `/health/...`、`/docs`、`/redoc`、`/openapi.json` |

规则必须按最长/更具体路径优先处理 API、服务 API 和后端文档/健康检查，再处理 `/gateway/` 前端 fallback。配置示意如下，OpenCode 应根据现有 Nginx 的容器名、网络和配置结构整合，不要直接照抄造成重复 `location`：

```nginx
location = /gateway {
    return 301 /gateway/;
}

location /gateway/api/ {
    proxy_pass http://agent-gateway-api:8000/api/;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
}

location /gateway/v1/ {
    proxy_pass http://agent-gateway-api:8000/v1/;
    proxy_http_version 1.1;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_buffering off;
    proxy_cache off;
    proxy_read_timeout 600s;
}

location ~ ^/gateway/(health|docs|redoc|openapi\.json)(/|$) {
    rewrite ^/gateway(/.*)$ $1 break;
    proxy_pass http://agent-gateway-api:8000;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
}

location ^~ /gateway/ {
    rewrite ^/gateway/(.*)$ /$1 break;
    proxy_pass http://agent-gateway-web:80;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
}
```

验证 Nginx 对 `proxy_pass` 尾部 URI 和 `rewrite` 的实际路径行为，尤其是不能将 `/gateway/api/...` 转成 `/gateway/api/...` 再交给后端。若前端容器的 Nginx `try_files` 无法正确处理剥掉前缀后的子路径，应调整其 SPA fallback，而不是把 API 请求交给前端。

## Docker 与网络要求

- 前端和后端由仓库的单一 `compose.yaml` 管理。
- 外部 Nginx 必须能在共享 Docker 网络中通过 `agent-gateway-web` 与 `agent-gateway-api` 访问容器。
- 后端端口继续只在 Docker 网络内开放；除非用户明确要求，不要把 `8000` 直接发布到公网。
- 保留现有 PostgreSQL 配置和持久化数据；不要运行会删除 volume 或重建数据库的命令。
- 后端 SSE/流式响应必须关闭 Nginx buffering。

## 验证清单

部署前先执行前端 lint/build、后端相关测试、`docker compose config`、`docker compose build`。部署后逐项验证：

1. `/gateway/` 返回控制台页面，静态资源均从 `/gateway/` 加载。
2. 登录、刷新和直接打开一个嵌套路由均成功。
3. 浏览器 Network 面板中的控制台请求走 `/gateway/api/...`，LLM/上下文请求走 `/gateway/v1/...`；没有请求到根路径 `/api/`、`/v1/`。
4. `/gateway/api/health/ready`（如健康路由适用）或转发后的 `/gateway/health/ready` 返回成功；`/gateway/docs` 可打开且文档列出的调用路径能在该前缀下执行。
5. 使用项目 API Key 调用 `/gateway/v1/...`，验证鉴权、错误响应及流式 SSE 首帧能正常到达。
6. 同域名的其他应用仍能使用自己的 `/api/`、`/v1/`，没有被网关 location 截获。
7. 用 `nginx -t` 检查配置后再 reload；检查网关容器健康状态和日志。保留部署前备份及回滚步骤。

## 完成时向用户汇报

列出修改文件和 Nginx 配置位置，说明外部到内部的路径映射、实际测试结果、未验证项目及回滚方法。不要声称已部署成功，除非服务器上的容器、Nginx 和 HTTPS 请求均经过验证。
