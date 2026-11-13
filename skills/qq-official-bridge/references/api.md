# QQ 官方云桥 HTTP v1

## 连接

REST 基地址为部署域名，例如 `https://qq-bridge.example.com`，不要附加 `/mcp`。所有 `/v1/*` 请求使用：

```text
Authorization: Bearer <QQ_BRIDGE_TOKEN>
```

`GET /health` 用于 HTTP 存活检查；`GET /v1/status` 显示 QQ 网关连接状态。`GET /v1/capabilities` 返回当前部署实际开放的场景、功能、配额和回复窗口，是工作流开始时发现能力的入口。

## 消息、历史与资料

### 新收到的消息

`GET /v1/messages?after=0&limit=50&detail=lite`，可选 `scene`、`chat_id`。`detail=lite` 返回精简结构（`from`/`text`/`at`/`attachments`/`can_reply`/`replies_left`/`reply_deadline`），`full` 返回原始归档字段。这是以桥内入站 `seq` 递增的游标接口，适合 heartbeat：处理完成后保存返回的 `next_cursor`。

`POST /v1/messages/{seq}/ack`，JSON `{}`，确认已处理且可重复调用；不会删除历史或发送消息。

### 会话与历史

- `GET /v1/sessions`：桥实际观察到的会话。
- `GET /v1/sessions/{scene}/{chat_id}/messages?after=0&limit=50`：保留的传统入站消息视图，使用入站 `seq`。
- `GET /v1/sessions/{scene}/{chat_id}/history?after=0&limit=50&direction=all`：会话历史。`direction` 可为 `all`、`in`、`out`，使用独立的 `history_seq`，不要与入站 `seq` 混用。

这些接口展示的是桥从官方事件接收并保存的内容，适合建立已观察会话的上下文。

### 缓存发送者资料

- `GET /v1/profiles?scene={scene}&chat_id={chat_id}&user_id={user_id}&limit=50&offset=0`
- `GET /v1/profiles/{scene}/{chat_id}/{user_id}`

资料来自官方事件的缓存。`sender.username` 为消息当时的昵称，`sender_profile` 和资料接口返回最新缓存；`member_role` 表示群内角色。`qq_number` 可能为 `null`；OpenID 保留为会话路由标识。实际字段取决于官方事件。

## 上传和发送

### 上传本地文件字节

`POST /v1/media`，multipart 字段名为 `file`。返回 `media_id`、`size`、`expires_at`。上传只准备媒体；随后将 `media_id` 放入发送请求。

```text
python scripts/qq_bridge.py upload --file "E:/Pictures/example.png"
```

### 被动回复

`POST /v1/messages/send`：

```json
{"idempotency_key":"reply-123-v1","reply_to":123,"text":"收到，谢谢","markdown":false}
```

媒体可采用已上传媒体：

```json
{"idempotency_key":"image-123-v1","reply_to":123,"media_id":"上传返回的ID","media_kind":"image","filename":"example.png"}
```

也可使用公开 HTTPS 直链。QQ 将原生获取媒体，因此不需要把媒体转换为 Base64：

```json
{"idempotency_key":"image-url-123-v1","reply_to":123,"media_url":"https://media.example.com/example.png","media_kind":"image","filename":"example.png"}
```

`filename` 可选，且可搭配 `media_id` 或 `media_url`。`media_kind` 可为 `image`、`voice`、`video`、`file`；每次最多一份媒体。

### 主动发送

主动发送不填 `reply_to`，使用目标场景和会话 ID：

```json
{"idempotency_key":"reminder-unique-id","scene":"c2c","chat_id":"目标OpenID","text":"提醒内容"}
```

服务端按环境变量中的精确允许名单验证目标。由既有本地 Agent heartbeat 唤醒 Agent 发起工作，不需要或启动本地 daemon。主动发送可用性、场景和额度以 `GET /v1/capabilities` 为准。

## 发送结果与幂等操作

发送返回 HTTP 202 表示任务已入队。通过 `GET /v1/sends/{idempotency_key}` 获得状态：

| 状态 | 含义 |
|---|---|
| `queued` | 已排队 |
| `sending` | 正在调用平台 |
| `sent` | 平台已受理 |
| `failed` | 确定失败 |
| `unknown` | 平台结果待核实 |

同一 key 且内容相同会返回原任务；同一 key 内容不同返回 409。网络超时后查询原 key，而不是创建新 key，可避免重复投递。

`error_details` 含官方业务码（850026 下载失败、850019 格式不支持、850031 超出大小等），`error` 给出对应含义。平台抓不到媒体链接时，桥会自动下载后改走分片上传重试。

回复窗口默认值为 C2C 3600 秒/4 次、群 300 秒/5 次、频道和 DM 300 秒/5 次。用户配置的较低上限可以生效；`capabilities` 的部署返回和 QQ 平台结果为最终依据。

## CLI 快速参考

```text
python scripts/qq_bridge.py capabilities
python scripts/qq_bridge.py profiles --scene c2c --chat-id <会话ID> --limit 50 --offset 0
python scripts/qq_bridge.py profile --scene c2c --chat-id <会话ID> --user-id <用户ID>
python scripts/qq_bridge.py history --scene c2c --chat-id <会话ID> --after 0 --direction all
python scripts/qq_bridge.py send --reply-to 123 --media-url "https://media.example.com/a.png" --kind image --filename a.png --key image-123
```

CLI 使用 Python 标准库，上传本地文件时发送实际 multipart 字节，并保留 HTTPS、无重定向凭证转发等连接保护。
