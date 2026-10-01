# Docker 部署（Ubuntu 服务器）

本文从代码仓库克隆后开始，使用同一个 `compose.yaml` 构建前端静态文件并运行 FastAPI 后端。网关项目不包含或启动 Nginx；服务器已有的 Nginx 由部署环境统一管理，负责直接托管前端文件并按路径反代 API。网关使用已有 PostgreSQL 数据库；当前应用没有 Redis 依赖，因此部署不会启动 Redis 容器。仓库内的 PostgreSQL 只用于本地开发，通过 `local-db` profile 显式启动。

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

首次启动时，`ADMIN_USERNAME` 和 `ADMIN_PASSWORD` 必须同时配置。管理员创建成功后，可从 `.env` 移除这两项；`JWT_SECRET_KEY` 必须长期保留不变。若应用不使用 LLM 供应商配置，可暂时不配置 `LLM_PROVIDER_SECRET_KEY`，但管理员供应商密钥管理功能将不可用。生产前端构建默认使用 `/gateway/` 基路径，`APP_ROOT_PATH=/gateway` 用于生成正确的后端 API 文档地址；若服务器选择其他公开前缀，需让 OpenCode 同步调整前端构建基路径、此变量和 Nginx 路由。

## 3. 构建、迁移并启动网关

```bash
bash deploy/deploy.sh
```

脚本会依次创建共享网络 `gateway-proxy`、检查 Compose 配置、构建后端镜像和前端静态文件、运行 `alembic upgrade head`，然后启动后端。前端构建产物写入仓库目录 `deploy/www/`，后端容器只在 Docker 网络中开放 `8000`；项目不运行 Nginx 容器，也不将后端端口映射到宿主机。

查看状态和日志：

```bash
docker compose ps
docker compose logs -f api
```

将 `deploy/www/` 配置为服务器现有 Nginx 可读的静态文件目录，并确保 Nginx 容器挂载该目录。将 Nginx 容器接入 `gateway-proxy`，使其能通过别名 `agent-gateway-api` 访问后端。公网路径目标是 `/gateway/` 提供控制台、`/gateway/api/` 转发控制台 API、`/gateway/v1/` 转发项目服务 API；外部前缀如何在 Nginx 中分流和改写，由服务器部署时结合现有配置处理。

在服务器本机经现有 Nginx 验证：

```bash
curl -fsS http://127.0.0.1:8080/gateway/health/live
curl -fsS http://127.0.0.1:8080/gateway/health/ready
```

`/gateway/health/ready` 会检查数据库连接。`8080` 是现有 Nginx 发布到宿主机的统一入口；网关后端端口只在 Docker 网络中开放，不直接暴露公网。

## 4. 接入服务器现有 Nginx

当前服务器已有 `nginx1.30` 容器。部署脚本会建立 Docker 网络 `gateway-proxy`，并把后端登记为 `agent-gateway-api`。首次部署后，将现有 Nginx 容器接入该网络，并将仓库中的 `deploy/www/` 以只读方式挂载到 Nginx 容器内用于静态托管：

```bash
sudo docker network connect gateway-proxy nginx1.30
```

若提示容器已在该网络中，无需重复连接。可以用下面的命令确认：

```bash
sudo docker inspect nginx1.30 --format '{{json .NetworkSettings.Networks}}'
```

服务器端 Nginx 由部署人员单独管理。请让服务器上的 OpenCode 先检查 Nginx 容器的启动来源、配置挂载、证书、其他站点和现有 location，再自行决定如何安全地把 `/gateway/` 静态页面、`/gateway/api/`、`/gateway/v1/` 及文档/健康检查路径接入现有 HTTPS `server`。需要保证 API 到后端时路径能匹配现有 `/api/`、`/v1/` 路由；SSE 路径关闭代理缓冲。不要覆盖整份 Nginx 配置、不要影响其他站点；修改前备份，先 `nginx -t` 再 reload。Nginx 容器若由 Compose 管理，应把 Docker 网络和静态目录挂载写回它的 Compose 配置，不能只对运行中的容器做临时连接。

```bash
sudo docker exec nginx1.30 nginx -t
sudo docker exec nginx1.30 nginx -s reload
```

如果 Nginx 容器被删除重建，需要再次将新容器接入 `gateway-proxy`；若它由 Compose 管理，建议把该外部网络加入 Nginx 自己的 Compose 配置，避免重建后断开。

## 5. 其他项目如何调用

其他机器上的项目使用你的 HTTPS 域名和 `/gateway/v1` 即可；如果暂时只有 8080 端口，则 URL 要包含 `:8080`，并先启用 HTTPS 再发送 API Key。它们不需要访问供应商 URL 或供应商 API Key，只需提交项目 API Key 和模型名：

```python
from openai import OpenAI

client = OpenAI(
    api_key="项目成员自己的项目 API Key",
    base_url="https://你的网关域名/gateway/v1",
)
response = client.chat.completions.create(
    model="volc/Doubao-Seed-2.0-mini",
    messages=[{"role": "user", "content": "你好"}],
    stream=True,
)
```

模型名不带供应商前缀时使用默认池；带已配置前缀时路由到该供应商组。项目必须先申请相应服务并分配额度。若另一个项目也运行在这台服务器且接入同一 Docker 网络，可以把 `base_url` 改成 `http://agent-gateway-api:8000/v1`，流量就留在 Docker 私有网络中；其他主机应使用 HTTPS 域名。

上下文服务也使用同一网关域名，例如 `https://你的网关域名/gateway/v1/context/templates`，通过 `Authorization: Bearer <项目 API Key>` 鉴权。API Key 只授予其所属项目的访问权限；按成员创建的 Key 会在日志中标识对应成员。

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
- 公网 HTTPS 由服务器现有 Nginx 或负载均衡器终止 TLS；Nginx 直接读取前端构建目录，并通过 `gateway-proxy` 网络反代到网关后端。正式开放前还应配置限流、证书续期和备份监控。Compose 不把后端 `8000` 映射到宿主机。

## 8. 常见问题

- **后端容器不健康**：查看 `docker compose logs api`，核对 `DATABASE_URL`、数据库防火墙和 `pg_hba.conf`。
- **连同机 PostgreSQL 超时**：确认数据库监听容器网关可达地址；`.env` 使用 `host.docker.internal`，不要填写容器内的 `127.0.0.1`。
- **迁移失败**：先保存完整错误和数据库备份；不要删除数据库 volume 或直接重建数据表。
- **网页打不开**：检查 `deploy/www/index.html` 是否存在、Nginx 是否挂载了 `deploy/www/`、现有 Nginx 的 8080 端口和服务器防火墙。
- **浏览器 API 调用失败**：确认现有 Nginx 的 `/gateway/api/` 与 `/gateway/v1/` 转发到 `agent-gateway-api:8000`，并检查后端日志中的 Trace ID。
