# 向量集合中的普通记录、父子关联与版本发布

## 记录语义

`POST /v1/vector-stores/collections/{collection}/upsert` 仍使用既有的 `vectors` 数组。每条记录必须带调用方稳定提供的 `id`，其唯一范围是项目 + collection。省略 `record_kind` 时按 `vector` 处理，因此旧客户端请求保持原语义。

- `record_kind: "vector"`：正文是调用方预先切好的文本片段，或者一个受支持的媒体输入。网关生成 Embedding，并把正文、向量和这条记录自己的 `metadata` 保存起来。文本不超过 collection 的 `chunk_size`，也不超过 6,000 个 Unicode 码位。
- `record_kind: "document"`：只保存正文和 metadata，不调用 Embedding，不扣 Embedding 额度，不创建 `rag_chunks` 行，也不初始化或改变 collection 向量维度。正文上限为 20,000 个 Unicode 码位（不是 UTF-8 字节数）。
- 父子关系可用于普通即时记录：父块与子片只需在同项目、同 collection 下关联 `parent_id`，不必提供版本字段。`namespace` 与 `document_id` 可不填；若提供则父子必须一致。只有需要跨批次发布时才需要完整版本字段。
- 两种记录共享同一张 `rag_documents` 外壳表；只有 vector 记录在 `rag_chunks` 表有向量行。没有用零向量代表普通记录。`modality` 独立保留，普通文本记录为 `text`。
- 请求体最多 4 MiB，每批 1–64 条；同一批 ID 必须唯一。父块需先单独写入，第一版不支持同批父子引用，也不支持 document 作为另一个记录的子项。
- 同类型、同 ID 的重复 upsert 是幂等替换。vector 与 document 之间禁止类型转换。已有成功记录不会在 Embedding 完成前被覆盖；数据库替换在一个事务中提交。外部 Embedding 调用无法参加数据库事务，若 Embedding 成功但数据库提交失败，用量可能已产生；调用方重试可能再次计量。

`parent_id` 和 `id` 使用同一种外部 ID，不是数据库 UUID。数据库外键将引用限制在同一 collection；API Key 将 collection 限制在所属项目。parent 必须是普通 `document` 记录。父子版本字段和发布状态必须一致。数据库触发器也会校验父记录类型、关联范围以及向量行只能属于 vector 记录。

## 写入示例

先写父记录：

```json
{
  "vectors": [
    {
      "id": "doc123-v2-parent0001",
      "record_kind": "document",
      "content": "完整父块正文",
      "metadata": {
        "document_id": "doc123",
        "source": "手册/安装.md",
        "record_type": "knowledge_parent"
      }
    }
  ]
}
```

再写子片：

```json
{
  "vectors": [
    {
      "id": "doc123-v2-child0001",
      "record_kind": "vector",
      "parent_id": "doc123-v2-parent0001",
      "content": "用于检索的小切片",
      "metadata": {
        "document_id": "doc123",
        "source": "手册/安装.md",
        "record_type": "knowledge_chunk"
      }
    }
  ]
}
```

父块和子片的 metadata 是相互独立的；网关不替调用方复制或合并 metadata，也不解析文档、切片、保存业务文档生命周期。

旧 upsert 响应为兼容仍返回内部数据库 UUID 字段 `id`，以及调用方外部 ID 字段 `external_id`。新客户端应统一使用 `external_id`；query/fetch 的 `id` 是外部 ID。`parent_id`、fetch 和 delete 也都使用外部 ID。

## 查询、fetch 与删除

- `POST /collections/{collection}/query` 只对已发布、具有有效向量的 vector 记录做相似度查询。结果含外部 `id`、`record_kind`、`parent_id`、正文、metadata 和 score；不会自动展开父块。metadata 仍使用 JSONB 包含匹配语义。
- `POST /collections/{collection}/fetch` 请求 `{ "ids": [...] }`，最多 256 个 ID。按首次输入顺序返回去重后的记录和 `missing_ids`；不存在、其他项目、其他 collection、暂存记录都以 missing 表示，不暴露其存在状态。返回完整正文和 metadata，不返回向量，不做 Embedding。API Key 鉴权和项目 collection 隔离与其他向量接口相同。
- `POST /collections/{collection}/delete` 按外部 ID 幂等删除，响应保留旧字段 `deleted`，并增加 `deleted_ids`、`missing_ids`。父记录仍被未同时删除的子记录引用时返回 409；同批包含父和全部子时先删子再删父。只影响当前项目和 collection。

常见错误使用现有 Gateway JSON 错误体（`detail.code`、`detail.message`）：`parent_record_not_found` 为 404；`parent_record_kind_invalid`、`parent_record_scope_conflict`、`record_kind_conflict`、`parent_record_in_use`、`published_version_immutable`、`published_version_retired`、`publication_history_unknown` 为 409；`request_too_large` 为 413；无效字段、批次重复 ID、同批父子引用等请求校验错误为 422；`staged_version_not_found` 为 404。错误响应不返回跨项目或跨 collection 的记录存在信息。

## 暂存与版本发布

不带版本字段的 upsert（包括父子记录）继续立即可检索。要避免分批写入时新旧版本同时可见，调用方为一个业务文档的每条父记录和子片提供相同的 `namespace`、`document_id`、`version_id`，并设置 `staged: true`。带版本字段的记录必须暂存，只有 publish 操作能启用它们。父记录仍先于子片写入。暂存记录不会参与 query 或普通 fetch。已发布版本不可直接改写：完全相同的 staged 请求重放会作为幂等成功返回；要修改内容必须使用新的 `version_id`。

全部记录写入成功后调用：

```http
POST /v1/vector-stores/collections/{collection}/publish
Authorization: Bearer <project-api-key>
Content-Type: application/json
```

```json
{
  "namespace": "support-kb",
  "document_id": "doc123",
  "version_id": "v2"
}
```

发布事务限定在该 API Key 所属项目、collection、namespace 和 document_id 内，原子地启用目标版本，并令该业务文档原先已发布的记录退出 query/fetch；旧版本可在发布成功后另行清理。目标版本重复发布是幂等的。发布接口无法判断调用方是否已经写齐了“预期全部记录”，完整性由调用方负责。若分批写入中途失败，不要发布该版本；继续补齐或删除暂存记录。

普通 API Key 的 fetch 不可见暂存记录。项目控制台的管理 API `POST /api/v1/rag/projects/{project_id}/knowledge-bases/{knowledge_base_id}/records/fetch-staged` 使用现有项目成员鉴权，并要求 owner/editor 写权限；它是管理员排查暂存内容的明确授权路径，不接受项目 API Key。

## 独立 Python SDK 适配契约

Gateway API 只修改本仓库；独立 SDK 位于 `D:\github\课程\gateway_api`，本次未修改。SDK 可在现有异步轻量客户端上增加：

- `add_records(records)`：按 64 条分批调用现有 upsert。`record_kind`、`parent_id`、`namespace`、`document_id`、`version_id`、`staged` 直接序列化；普通记录不触发客户端 Embedding。先提交父记录，再提交引用它的子记录。
- 现有 `add_vectors(...)`：继续默认写 `record_kind="vector"`，维持现有调用兼容。
- `fetch_records(ids)`：调用 `/collections/{collection}/fetch`，返回 `records` 与 `missing_ids`。
- `publish_version(namespace, document_id, version_id)`：调用 `/collections/{collection}/publish`。
- 现有 `delete(ids)`：接受 `deleted_ids`、`missing_ids`，同时兼容旧 `deleted` 计数字段。

SDK 不负责切片、父块构造、业务数据库或文档生命周期。

## 迁移与回滚

增量迁移把既有 `rag_documents` 标记为 `record_kind=vector`、已发布；不会删除或重建现有向量。备份后，在 backend 目录执行：

```powershell
uv run alembic upgrade head
```

`20261010_0042` 单独增加 `was_published` 状态列，并使用 `ADD COLUMN IF NOT EXISTS`：已经执行过不含该列的旧版 `0041` 的环境会补列；全新升级或曾执行包含该列的开发版 `0041` 也可安全升级。迁移时，可见记录标记为已发布；旧的隐藏版本无法从 `is_published` 判断是暂存还是退役，故标记为未知（NULL），不会错误地全部当成历史版本。未知记录禁止编辑、原 ID 重写和发布；应使用新的 `version_id` 与外部 ID 重建，或由管理员依据外部审计信息核实后修复状态。

`0042` 回滚会在仍有版本记录时拒绝执行，以免丢失发布历史。需先导出并清理这些记录，再回滚；`0041` 自身也会阻止带版本字段的数据回滚。

回滚到上一迁移：

```powershell
uv run alembic downgrade 20261004_0040
```

`downgrade()` 会先执行数据库级预检；只要存在普通记录、父子关系、namespace/document/version 字段或未发布记录，就会抛错停止，不删除数据也不继续删列。需先导出并清理这些新能力产生的记录，再回滚。检查通过后，回滚仅删除新增字段、约束、索引和触发器，不删除 `rag_chunks` 中已有向量。历史版本即使当前已隐藏，也因保留版本字段而会阻止回滚；清理前应导出需要保留的数据。

## 第一版边界

- 不支持自动生成 ID、同批父子写入、嵌套 document、媒体普通记录或暂存版本自动补全。
- 数据库写入批次原子；多次 upsert 不原子。只有显式暂存并调用 publish 才实现跨批次的可见性切换。
- 外部 Embedding 额度不具备跨服务回滚保证；分批重试可能重复调用上游。
- 真实环境升级前应使用隔离 PostgreSQL 和测试 collection 验证迁移、约束、发布与重试；测试不得指向生产数据库。
