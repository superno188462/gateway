# Agent Gateway 服务器部署文档

## 架构

```
浏览器 ──▶ nginx1.30 容器 (:8080)
              ├── /gateway/           → 静态文件 /usr/share/nginx/html/gateway/
              ├── /gateway/api/       → agent-gateway-api:8000 (后端 /api/)
              ├── /gateway/v1/        → agent-gateway-api:8000 (后端 /v1/，SSE关缓冲)
              └── /gateway/health|docs|redoc|openapi.json → agent-gateway-api:8000
```

- 前端：构建产物由宿主机 nginx 直接托管，无前端容器
- 后端：FastAPI + uvicorn，Docker 网络 `gateway-proxy` 内别名 `agent-gateway-api`
- 数据库：复用现有 `pg16` 容器，库名 `gateway`

---

## 一、首次部署

### 1. 创建数据库

```bash
sudo docker exec pg16 psql -U postgres -c "CREATE DATABASE gateway;"
```

### 2. 配置环境变量

```bash
cd ~/usr/docker/gateway
cp .env.server.example .env
vi .env
```

关键配置：

```env
DATABASE_URL=postgresql+asyncpg://postgres:URL_ENCODED_PASSWORD@host.docker.internal:5432/gateway
APP_ENV=production
APP_ROOT_PATH=/gateway
JWT_SECRET_KEY=<openssl rand -hex 32>
API_KEY_SECRET_KEY=<openssl rand -hex 32>
LLM_PROVIDER_SECRET_KEY=<openssl rand -hex 32>
ADMIN_USERNAME=admin
ADMIN_PASSWORD=<强密码>
```

> 数据库用户名/口令以实际为准，请勿把真实口令提交到仓库。口令中的 `@`、`:`、`/`、`#` 等保留字符需要 URL 编码（例如 `@` 写成 `%40`）。

```bash
chmod 600 .env
```

### 3. 构建前端产物

```bash
cd frontend
npm install
npm run build
cd ..
mkdir -p deploy/www
cp -a frontend/dist/. deploy/www/
```

> 注意：`frontend/package.json` 里不能有 `@rollup/rollup-win32-x64-msvc`，否则 Linux 下 npm install 报 EBADPLATFORM。

### 4. 构建后端镜像并启动

```bash
cd ~/usr/docker/gateway

# 创建 Docker 网络（已存在则跳过）
sudo docker network create gateway-proxy

# 构建后端镜像
sudo docker compose build --pull=false api

# 跑数据库迁移
sudo docker compose run --rm api alembic upgrade head

# 启动后端容器
sudo docker compose up -d
```

### 5. 接入 nginx

```bash
# 把 nginx 容器接入 gateway-proxy 网络
sudo docker network connect gateway-proxy nginx1.30

# 拷贝前端产物到 nginx 静态目录
sudo mkdir -p ~/usr/database/nginx/nginx1.30.3/html/gateway
sudo rm -rf ~/usr/database/nginx/nginx1.30.3/html/gateway/*
sudo cp -a deploy/www/. ~/usr/database/nginx/nginx1.30.3/html/gateway/
```

### 6. nginx 配置

配置文件位置：`~/usr/database/nginx/nginx1.30.3/conf.d/gateway.locations`

在 `default.conf` 中 include：

```nginx
include /etc/nginx/conf.d/gateway.locations;
```

location 规则：

| 外部路径 | 后端路径 | 说明 |
|---|---|---|
| `/gateway/` | 静态文件 | SPA，fallback 到 index.html |
| `/gateway/api/health/` | `/health/` | 健康检查 rewrite |
| `/gateway/api/` | `/api/` | 控制台 API |
| `/gateway/v1/` | `/v1/` | LLM 网关 + Context，SSE 关缓冲 |
| `/gateway/health\|docs\|redoc\|openapi.json` | 对应路径 | 文档和根健康检查 |

### 7. 重载 nginx

```bash
sudo docker exec nginx1.30 nginx -t
sudo docker exec nginx1.30 nginx -s reload
```

---

## 二、验证

```bash
# 前端
curl -I http://127.0.0.1:8080/gateway/

# 健康检查
curl http://127.0.0.1:8080/gateway/api/health/live
curl http://127.0.0.1:8080/gateway/api/health/ready

# Swagger
curl -I http://127.0.0.1:8080/gateway/docs

# 登录
curl -X POST http://127.0.0.1:8080/gateway/api/v1/auth/login \
  -H "Content-Type: application/json" \
  -d '{"username":"admin","password":"***"}'
```

---

## 三、日常升级

```bash
cd ~/usr/docker/gateway
git pull --ff-only

# 1. 更新前端
cd frontend && npm install && npm run build && cd ..
rm -rf deploy/www/*
cp -a frontend/dist/. deploy/www/

# 2. 更新后端
sudo docker compose build --pull=false api
sudo docker compose run --rm api alembic upgrade head
sudo docker compose up -d

# 3. 同步前端到 nginx
sudo rm -rf ~/usr/database/nginx/nginx1.30.3/html/gateway/*
sudo cp -a deploy/www/. ~/usr/database/nginx/nginx1.30.3/html/gateway/
```

---

## 四、回滚

```bash
# 回滚后端
cd ~/usr/docker/gateway
git checkout <旧commit>
sudo docker compose build --pull=false api
sudo docker compose up -d

# 回滚前端
cd frontend && npm run build && cd ..
rm -rf deploy/www/* && cp -a frontend/dist/. deploy/www/
sudo rm -rf ~/usr/database/nginx/nginx1.30.3/html/gateway/*
sudo cp -a deploy/www/. ~/usr/database/nginx/nginx1.30.3/html/gateway/
```

> 数据库迁移不保证可逆，升级前务必备份 PostgreSQL。

---

## 五、常见问题

| 问题 | 排查 |
|---|---|
| 后端容器不健康 | `docker compose logs api`，检查 DATABASE_URL、pg_hba.conf |
| 前端 404 | 确认 `deploy/www/index.html` 存在，nginx html/gateway/ 已拷贝 |
| API 404 | 确认 nginx 已接入 gateway-proxy 网络：`docker inspect nginx1.30` |
| SSE 卡住 | 确认 `/gateway/v1/` location 有 `proxy_buffering off` |
| 数据库连接超时 | `.env` 用 `host.docker.internal`，不要用 127.0.0.1 |
