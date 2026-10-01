# Docker 部署（Ubuntu 服务器）

本文从代码仓库克隆后开始，使用同一个 `compose.yaml` 部署 Agent Gateway 前端和 FastAPI 后端。网关使用已有 PostgreSQL 数据库；当前应用没有 Redis 依赖，因此部署不会启动 Redis 容器。仓库内的 PostgreSQL 只用于本地开发，通过 `local-db` profile 显式启动。

## 1. 服务器准备

准备一台已安装 Git、Docker Engine 和 Docker Compose 插件的 Linux 服务器。确认以下命令可运行：

```bash
git --version
docker --version
docker compose version
```

数据库应使用 PostgreSQL 16 或兼容版本，并允许服务器 Docker 容器访问。若 PostgreSQL 安装在同一台服务器，示例连接地址使用 `host.docker.internal`；Compose 已添加 Linux `host-gateway` 映射。若数据库在其他主机，请在 `.env` 中填写数据库主机名，并在数据库防火墙及 `pg_hba.conf` 中允许服务器访问。数据库本身不要向公网开放。

## 2. 克隆代码并配置密钥

按实际仓库访问方式选择 SSH 或 HTTPS：

```bash
git clone git@github.com:superno188462/gateway.git
cd gateway
cp .env.server.example .env
```

生成密钥：

```bash
openssl rand -hex 32
```

把命令生成的不同随机值分别填入 `.env` 的 `JWT_SECRET_KEY`、`API_KEY_SECRET_KEY`、`LLM_PROVIDER_SECRET_KEY`。三个变量用途不同，不能共用。修改管理员初始密码、数据库用户名、数据库名和连接地址。数据库密码中的 `@`、`:`、`/`、`#` 等保留字符需要进行 URL 编码。

保护配置文件并确认 Git 不会提交它：

```bash
chmod 600 .env
git check-ignore .env
```

首次启动时，`ADMIN_USERNAME` 和 `ADMIN_PASSWORD` 必须同时配置。管理员创建成功后，可从 `.env` 移除这两项；`JWT_SECRET_KEY` 必须长期保留不变。若应用不使用 LLM 供应商配置，可暂时不配置 `LLM_PROVIDER_SECRET_KEY`，但管理员供应商密钥管理功能将不可用。

## 3. 构建、迁移并启动网关

```bash
bash deploy/deploy.sh
```

脚本会依次创建共享反代网络 `gateway-proxy`、检查 Compose 配置、构建前后端镜像、运行 `alembic upgrade head`，然后启动服务。Compose 内部前端容器监听 `80`、后端监听 `8000`；两个端口都不映射到宿主机，由现有 Nginx 通过 Docker 网络访问。

查看状态和日志：

```bash
docker compose ps
docker compose logs -f api
docker compose logs -f web
```

在服务器本机验证：

```bash
curl -fsS http://127.0.0.1:8080/health/live
curl -fsS http://127.0.0.1:8080/health/ready
```

`/health/ready` 会检查数据库连接。`8080` 是现有 Nginx 发布到宿主机的统一入口；网关的前端和后端端口只在 Docker 网络中开放，不直接暴露公网。

## 4. 接入服务器现有 Nginx

当前服务器已有 `nginx1.30` 容器。部署脚本会建立 Docker 网络 `gateway-proxy`，并把前端和后端容器分别登记为 `agent-gateway-web` 和 `agent-gateway-api`。首次部署后，将现有 Nginx 容器接入该网络：

```bash
sudo docker network connect gateway-proxy nginx1.30
```

若提示容器已在该网络中，无需重复连接。可以用下面的命令确认：

```bash
sudo docker inspect nginx1.30 --format '{{json .NetworkSettings.Networks}}'
```

在现有 Nginx 对应域名的 HTTPS `server` 块中配置路径分流：网页 `/` 转给前端 `agent-gateway-web:80`；`/api/`、`/v1/` 和后端文档/健康检查路径转给 `agent-gateway-api:8000`。保留你现有的证书和 TLS 配置，并加入：

```nginx
location /api/ {
    proxy_pass http://agent-gateway-api:8000;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
}

location /v1/ {
    proxy_pass http://agent-gateway-api:8000;
    proxy_http_version 1.1;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_buffering off;
    proxy_cache off;
    proxy_read_timeout 600s;
}

location ~ ^/(health|docs|redoc|openapi\.json)(/|$) {
    proxy_pass http://agent-gateway-api:8000;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
}

location / {
    proxy_pass http://agent-gateway-web:80;
    proxy_http_version 1.1;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
}
```

`/v1/` 是网关 API 路径，关闭代理缓冲可让 SSE 流式内容及时到达调用方；`/` 提供控制台静态页面，`/api/` 由外层 Nginx 直接转发给后端。若原配置已有相同的 `location`，请合并或替换，避免重复定义。完成后在现有 Nginx 容器内检查并重载配置，例如：

```bash
sudo docker exec nginx1.30 nginx -t
sudo docker exec nginx1.30 nginx -s reload
```

如果 Nginx 容器被删除重建，需要再次将新容器接入 `gateway-proxy`；若它由 Compose 管理，建议把该外部网络加入 Nginx 自己的 Compose 配置，避免重建后断开。

## 5. 其他项目如何调用

其他机器上的项目使用你的 HTTPS 域名即可；如果暂时只有 8080 端口，则 URL 要包含 `:8080`，并先启用 HTTPS 再发送 API Key。它们不需要访问供应商 URL 或供应商 API Key，只需提交项目 API Key 和模型名：

```python
from openai import OpenAI

client = OpenAI(
    api_key="项目成员自己的项目 API Key",
    base_url="https://你的网关域名/v1",
)
response = client.chat.completions.create(
    model="volc/Doubao-Seed-2.0-mini",
    messages=[{"role": "user", "content": "你好"}],
    stream=True,
)
```

模型名不带供应商前缀时使用默认池；带已配置前缀时路由到该供应商组。项目必须先申请相应服务并分配额度。若另一个项目也运行在这台服务器且接入同一 Docker 网络，可以把 `base_url` 改成 `http://agent-gateway-api:8000/v1`，流量就留在 Docker 私有网络中；其他主机应使用 HTTPS 域名。

上下文服务也使用同一网关域名，例如 `https://你的网关域名/v1/context/templates`，通过 `Authorization: Bearer <项目 API Key>` 鉴权。API Key 只授予其所属项目的访问权限；按成员创建的 Key 会在日志中标识对应成员。

## 6. 更新代码和回滚注意事项

```bash
git pull --ff-only
bash deploy/deploy.sh
```

脚本在替换应用容器前运行数据库迁移。数据库迁移不保证可逆；升级前请先备份 PostgreSQL，并按本项目迁移的向前兼容策略处理回滚，不能只回退容器镜像。

## 7. 备份与安全

- 定期备份 PostgreSQL，并在独立位置验证可恢复性。
- 将 `.env` 和数据库备份限制给部署管理员访问；不要提交或发送真实凭据。
- `API_KEY_SECRET_KEY` 和 `LLM_PROVIDER_SECRET_KEY` 必须备份且保持稳定；更换会导致已有密钥密文无法解密。
- 对公网只开放 HTTPS 端口；不要开放 PostgreSQL 端口。
- `docker compose` 命令可读取 `.env`，但操作系统管理员仍能检查容器配置。限制服务器 Docker 管理权限。
- 公网 HTTPS 应由现有 Nginx 或负载均衡器终止 TLS，再通过 `gateway-proxy` 网络转发到网关容器；正式开放前还应配置限流、证书续期和备份监控。Compose 不把前端 `80` 或后端 `8000` 映射到宿主机。

## 8. 常见问题

- **后端容器不健康**：查看 `docker compose logs api`，核对 `DATABASE_URL`、数据库防火墙和 `pg_hba.conf`。
- **连同机 PostgreSQL 超时**：确认数据库监听容器网关可达地址；`.env` 使用 `host.docker.internal`，不要填写容器内的 `127.0.0.1`。
- **迁移失败**：先保存完整错误和数据库备份；不要删除数据库 volume 或直接重建数据表。
- **网页打不开**：检查现有 Nginx 的 8080 端口、服务器防火墙和 `docker compose ps`；确认 Nginx 容器已接入 `gateway-proxy` 网络且上游别名正确。
- **浏览器 API 调用失败**：确认前端 Nginx 的 `/api/` 与 `/v1/` 代理路径可用，并检查后端日志中的 Trace ID。
