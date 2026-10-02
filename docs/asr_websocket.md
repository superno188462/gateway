# 实时 ASR WebSocket 接口

网关提供统一的实时语音识别接口，当前由网关内部适配火山引擎豆包 Seed ASR 2.0。业务调用方只连接网关并传公开 `model`；火山 API Key、上游 WebSocket URL 和 Resource ID 由管理员在网关“ASR API”页面配置并加密保存。

## 连接与鉴权

开发环境地址为 `ws://127.0.0.1:8000/v1/audio/stream`。生产环境地址取决于网关的公开部署路径，例如 `wss://gateway.example.com/gateway/v1/audio/stream`。业务程序还需使用所属项目的网关 API Key 完成项目鉴权；它不是火山 API Key。

支持以下两种鉴权方式：

- 常规 WebSocket 客户端在握手请求中发送 `Authorization: Bearer <项目 API Key>`。
- 浏览器原生 WebSocket 无法设置 Authorization header，可传子协议 `bearer` 和项目 API Key，例如 `new WebSocket(url, ["bearer", projectApiKey])`。服务端只选择 `bearer` 子协议，不回显 Key。

不要把 API Key 放入 URL 查询参数。业务模型连接配置可保存：

```dotenv
ASR_BASE_URL=ws://127.0.0.1:8000/v1/audio/stream
ASR_MODEL=doubao-seed-asr-2.0
```

项目 API Key 仍按应用已有的网关鉴权方式安全提供（例如由后端凭据管理传入握手 header），它不属于火山模型配置。

## 客户端消息协议

连接后第一条必须为 `start` JSON 消息：

```json
{
  "type": "start",
  "model": "doubao-seed-asr-2.0",
  "audio": {
    "format": "pcm",
    "sample_rate": 16000,
    "channels": 1,
    "bits": 16,
    "endianness": "little"
  },
  "language": "zh-CN",
  "prompt": "可选的领域词汇提示"
}
```

`audio`、`language` 和 `prompt` 均可省略。默认音频格式固定为裸 PCM、16 kHz、单声道、16-bit signed little-endian（s16le）；目前不支持在实时 WebSocket 接口中切换采样率、声道数、编码或字节序。`language` 最长 20 个字符，`prompt` 最长 4000 个字符。`model` 必须与管理员配置的公开模型名一致。旧式 `volc/<模型名>` 仍可作为兼容输入，但新接入应使用不带供应商前缀的公开模型名。

随后持续发送 WebSocket **二进制帧**，每帧为连续 PCM 字节。每帧必须非空、长度为偶数且不超过 1 MiB；建议每帧 20–100 ms（640–3200 字节），不要在帧之间插入 WAV 文件头。网关按收到的 PCM 字节数计算时长并预留 ASR 秒额度，音频数据和识别文本不会写入请求日志。

音频发送完毕后发送文本 JSON：

```json
{"type":"end"}
```

静音判停可以在连接保持期间返回某句话的 final。`end` 用于通知网关整条音频流已结束，网关随后发送上游结束帧并等待会话完成；重复发送 `end` 是幂等的，`end` 后不得再发音频。空音频会以错误事件结束。断开连接会中止上游并释放未使用的额度预留。

## 网关事件

网关会先返回 `session.started`，之后根据上游结果即时返回 partial/final 事件。每个事件带有 `utterance_id`；上游提供时间戳时也会带 `start_ms`、`end_ms`：

```json
{"type":"session.started","request_id":"trace_...","model":"doubao-seed-asr-2.0"}
{"type":"transcript.partial","utterance_id":"...","text":"当前语句的变化中内容","start_ms":2400,"end_ms":2800,"request_id":"trace_..."}
{"type":"transcript.final","utterance_id":"...","text":"当前语句最终内容","start_ms":2400,"end_ms":3100,"request_id":"trace_..."}
```

完成且额度结算后返回：

```json
{
  "type":"session.completed",
  "request_id":"trace_...",
  "audio_seconds": 2.4,
  "billed_seconds": 3
}
```

`transcript.partial` 和 `transcript.final` 都只表示当前语句，不包含已确认的前序语句。partial 只能用于更新界面，不能触发 Agent 的 LLM 轮次。收到重复上游快照时，网关沿用同一 `utterance_id` 并抑制重复 final；后续再次说出相同文本时，火山返回的 `start_time` 不同，因此会获得新的 ID。ID 由本次网关 `request_id`、火山语句起始时间和说话人标识做 UUIDv5 生成。

火山 `show_utterances=true` 的响应包含语句文本、`start_time`、`end_time` 和 `definite`。网关逐句转发 definite 语句，不把 `result.text` 的累计全文当作新 final；没有 utterances 的兼容响应只按此前已确认文本剥离增量。火山语句时间戳缺失时会退回当前 utterance 列表位置生成 ID，因此需要供应商提供 `start_time` 才能可靠识别“相同内容后来再次说出”的新语句。

静音判停产生的语句 final 不会结束网关 WebSocket。调用方应继续保持连接并可继续发送下一句话；只有 `{"type":"end"}` 后收到上游会话最后一包时，网关才发送 `session.completed` 并结算额度。计费按整秒向上取整，不足一秒按一秒计费。

错误统一为：

```json
{
  "type":"error",
  "code":"UPSTREAM_TIMEOUT",
  "message":"等待火山 ASR 实时结果超时",
  "request_id":"trace_..."
}
```

常见错误码包括 `INVALID_API_KEY`、`MODEL_NOT_CONFIGURED`、`SERVICE_NOT_ENABLED`、`PROJECT_QUOTA_EXCEEDED`、`USER_QUOTA_EXCEEDED`、`INVALID_AUDIO_CHUNK`、`EMPTY_AUDIO`、`UPSTREAM_AUTHENTICATION_FAILED`、`UPSTREAM_TIMEOUT` 和 `ASR_INTERNAL_ERROR`。错误消息可用于定位问题；请使用事件中的 `request_id` 查询操作日志和技术日志。鉴权失败使用 WebSocket close code 4401，违反协议使用 1008，上游/服务故障使用 1011，临时服务不可用使用 1013。

## 网关内部配置

管理员在控制台“ASR API”新增火山引擎连接，配置公开模型名、Resource ID、实时流 URL 和 API Key。实时上游 URL 通常为 `wss://openspeech.bytedance.com/api/v3/plan/sauc/bigmodel_async`，Resource ID 根据火山账户所购资源填写，例如 `volc.seedasr.sauc.duration`。API Key 由网关服务端加密存储，不需要写入调用方环境变量。部署必须配置 `LLM_PROVIDER_SECRET_KEY` 以启用现有的供应商密钥加密机制，并运行数据库迁移：

```powershell
cd backend
uv run alembic upgrade head
```

项目还需申请 ASR 服务并分配音频秒数额度。启动方式与网关其他接口一致：

```powershell
cd backend
uv run uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

## 最小调用示例

安装 Python 客户端依赖 `pip install websockets`，准备裸 PCM s16le 文件后运行：

```powershell
$env:GATEWAY_API_KEY = "<项目 API Key>"
python docs/asr_websocket_client.py --url ws://127.0.0.1:8000/v1/audio/stream --model doubao-seed-asr-2.0 --file speech-16k-mono-s16le.pcm
```

脚本以 100 ms 块发送文件，发送 `end` 后持续打印中间结果、最终结果和错误事件。实时场景可以复用相同协议，按麦克风采集顺序发送 PCM 块。

## 验证步骤

1. 检查 `LLM_PROVIDER_SECRET_KEY` 已配置，ASR 数据库迁移已执行。
2. 管理员添加火山 ASR 实时上游连接，并确认模型状态启用。
3. 为测试项目申请 ASR 服务并配置用户/项目秒数额度，创建项目 API Key。
4. 用同一个项目 API Key 运行上面的 PCM 示例。
5. 确认能先收到 `session.started`，随后收到一条或多条 `transcript.partial` / `transcript.final`，最后收到 `session.completed`。
6. 查询项目操作日志和管理员技术日志，使用 `request_id`/Trace ID 关联；确认按整秒结算，并确认日志不包含密钥、音频或识别正文。
7. 分别用无效 Key、未配置模型、无额度、空 PCM、错误字节长度及提前断开连接验证错误事件和额度释放。
