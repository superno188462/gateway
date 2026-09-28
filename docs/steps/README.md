# 实施步骤目录

这里记录“如何一步步开发和验收”，与 `docs/` 根目录下的架构、产品设计和决策文档分开。

## 使用方式

每次只执行一个阶段：

1. 阅读 `docs/PROJECT_BRIEF.md`，确认产品范围。
2. 阅读 `docs/artifacts/backend-architecture.md` 和 `docs/decisions.md`，确认边界和约束。
3. 阅读本目录对应阶段的执行要求。
4. 先更新契约和验收场景，再实现代码。
5. 先验证后端，再实现前端。
6. 更新 `docs/status.md`，记录实际结果。
7. 当前阶段验收通过后，才进入下一阶段。

## 阶段顺序

| 顺序 | 阶段 | 主要步骤 | 当前状态 |
|---|---|---|---|
| 1 | P0 | 规格、领域边界、数据模型、OpenAPI、UI 设计 | 已完成 |
| 2 | B0 | FastAPI、配置、DI、PostgreSQL、Alembic、健康检查 | 已完成 |
| 3 | F0 | 前端工程、路由、布局、类型安全 API Client | 已完成 |
| 4 | A1 | 统一登录、角色、JWT、认证依赖 | 已完成 |
| 5 | A2 | 项目 CRUD、权限边界和项目隔离 | 已完成 |
| 6 | A3 | API Key 创建、哈希、撤销和调用认证 | 未开始 |
| 7 | A4 | Mock Provider、兼容接口、token 用量和调用记录 | 未开始 |
| 8 | A5 | 日志查询、筛选、分页、用量汇总和费用展示 | 未开始 |
| 9 | A6 | 模板、记忆、目录约束和并发冲突 | 未开始 |
| 10 | A7 | 公网部署、HTTPS、限流、备份、回滚和安全验收 | 未开始 |

## 本地运行约定

后端：

```powershell
cd backend
uv sync --all-groups
uv run alembic upgrade head
uv run python main.py
```

前端在 F0 完成后：

```powershell
npm install
npm run dev
```

本地开发不依赖 Docker；服务器部署脚本在 A7 阶段准备。

## 阶段执行手册

详细的 Codex 执行模板、阶段提示词和验收提示词见：

- [CODEX_RUNBOOK.md](./CODEX_RUNBOOK.md)
- [PROJECT_BRIEF.md](../PROJECT_BRIEF.md)
- [backend-architecture.md](../artifacts/backend-architecture.md)
- [status.md](../status.md)
