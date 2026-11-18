# QQ 云桥 API

## 连接

同一端口提供 MCP Streamable HTTP `/mcp`（及 `/mcp/`）与 HTTP `/v1/*`。私密配置请求头 `Authorization: Bearer <BRIDGE_API_TOKEN>`。`GET /health` 是公开存活检查，QQ 状态使用 `/v1/status`。时间字段为 ISO8601。

## MCP 工具

| 工具 | 用途 | REST |
|---|---|---|
| qq_status | QQ 状态、队列、最近诊断 | GET /v1/status |
| qq_get_capabilities | 功能、上传限额、回复规则、主动配置 | GET /v1/capabilities |
| qq_get_messages | 游标获取收到的消息 | GET /v1/messages |
| qq_ack_message | 标记处理 | POST /v1/messages/{seq}/ack |
| qq_list_sessions | 会话资料与主动联系状态 | GET /v1/sessions |
| qq_get_session_messages | 会话收到的消息 | GET /v1/sessions/{scene}/{chat_id}/messages |
| qq_get_session_history | 双向收发记录 | GET /v1/sessions/{scene}/{chat_id}/history |
| qq_get_profiles | 分页查看资料缓存 | GET /v1/profiles |
| qq_get_profile | 一位发言人的完整缓存资料 | GET /v1/profiles/{scene}/{chat_id}/{user_id} |
| qq_get_info | 按需查看群、群成员或私聊对象的缓存资料和头像 | GET /v1/info |
| qq_send_message | 文字、URL 媒体、上传文件发送 | POST /v1/messages/send |
| qq_get_send_result | 发送阶段与结果 | GET /v1/sends/{key} |
| qq_upload_media | 标准 Base64 小文件上传 | POST /v1/media multipart |

MCP description 聚焦功能与参数。以下约定解释状态、权限与游标。工具响应只返回一份 JSON 文本，保持精简。

### detail=lite 与 detail=full

MCP 工具默认 `detail=lite`，返回 Agent 实际需要的内容；REST 默认 `full`，保持原始归档结构。两者可随时切换。

lite 消息结构：

```json
{"seq": 9, "text": "hi",
 "from": {"id": "...", "name": "Komorebi", "role": "owner"},
 "at": "2026-11-13T17:54:38+08:00",
 "attachments": [{"kind": "image", "filename": "a.jpg", "url": "https://...", "size": 184616}],
 "can_reply": true, "replies_left": 5, "reply_deadline": "2026-11-13T09:59:38+00:00"}
```

- `from` 保留 `id`、`name`、`role`。头像由 `qq_get_info` 按需返回，`full` 原有头像字段和消息附件 URL 保留。
- 不再返回 `message_id`（回复用 `seq`）、`received_at`、`event_type`、`name_source`、`username_source` 等内部字段。
- 空值字段不输出；`acked` 仅在已确认时出现。
- 同一会话的 `scene`/`chat_id` 提升为响应级 `chat`，不再逐条重复。
- 用 `reply_deadline` 判断时效，不再返回每次调用都变化的秒级倒计时，响应可稳定比较。
- `qq_get_send_result` 精简为 `key/status/stage/msg_seq/error/codes`；`qq_get_capabilities` 只列可用场景。

头像 URL 长 127 字符时，200 条都带头像的消息仅链接就少 25,400 字符；紧凑 JSON 连同字段名和标点共少 27,800 字符。

## 消息与身份

`GET /v1/messages?after=0&limit=50&scene=c2c&chat_id=...` 返回 `{items,next_cursor}`，scene/chat_id 可选，limit 1..200，after 排他。消息含：

- `seq`、`message_id`、`scene`、`chat_id`、`user_id`、`content`、`timestamp`、`received_at`、`acked`。
- `attachments`：平台实际提供的 filename/content_type/url/size，可能含 width/height/voice_wav_url/asr_refer_text。
- `asr_text`、引用元素、mentions 等可用数据。
- `sender`：消息当时的发言人资料快照。
- `sender_profile`：服务端最新缓存资料。
- `reply_deadline`、`reply_remaining_seconds`、`replies_remaining`、`can_reply`、`proactive_allowed`。

倒计时与剩余次数是动态值，前后两次请求可能不同。昵称来源保留为 official_event，缺失值为 null/未知；稳定成员 ID 用于区分同名。OpenID 与 QQ 号各自有独立语义，QQ 号不通过标识符推算。

`POST /v1/messages/{seq}/ack` 幂等返回 `{seq,acked:true}`，独立于发送和游标。处理完成后由 Agent 保存 next_cursor。

`GET /v1/profiles?scene=group&chat_id=...&user_id=...&limit=50&offset=0` 按会话筛选自动积累的资料；`GET /v1/profiles/{scene}/{chat_id}/{user_id}` 查询指定成员。官方事件有名字时自动更新；独立的官方全群成员查询属内邀权限，基础资料接口读取本桥缓存。

### 按需资料查询

`qq_get_info` / `GET /v1/info` 使用相同参数：

| 目标 | 参数 |
|---|---|
| 私聊对象 | `scene=c2c`、`chat_id` |
| 群资料 | `scene=group`、`chat_id` |
| 群成员 | `scene=group`、`chat_id`、`user_id`（消息 `from.id`） |

用户资料返回 `scene`、`chat_id`、`user_id`，以及已缓存的 `name`、`role`、`avatar`、`bot`、`updated_at`。群资料返回 `info_status`：`cached`（已缓存）、`not_cached`（已知会话，尚无群资料）、`unavailable`（已记录查询不可用），并附可用的 `name`、`member_count`、`description`、`category`、`tags` 和 `avatar`。仅实际提供并缓存的头像 URL 才返回；[官方群基本信息](https://bot.q.qq.com/wiki/develop/api-v2/autogen/api/v2_groups_group_openid_info.get.html)目前未列出头像字段。查找限定在指定会话，未观察到的成员或未知会话返回 404。

## 会话与双向历史

`GET /v1/sessions` 返回会话统计、可用显示名称及 proactive_allowed。群会话另带 `group`：桥在首次收到该群消息时调用官方 `/v2/groups/{group_openid}/info` 取群名、简介、分类、标签与人数并缓存；该接口为白名单能力，未获权限时 `group` 为 `{"unavailable": true}`。

- 旧接口 `/v1/sessions/{scene}/{chat_id}/messages` 使用入站 `seq`，语义保持兼容。
- 双向 `/v1/sessions/{scene}/{chat_id}/history?after=0&limit=50&direction=all` 使用独立 `history_seq`；direction 可选 all/in/out。
- 入站记录保留 `inbound_seq`；机器人发言含 idempotency_key、text、媒体类型与状态。
- `delivered:true` 对应平台已受理的 sent；其他发送结果可显示为 send_attempt。

入站与双向历史的 next_cursor 分开保存；回复始终使用原消息 inbound seq。历史迁移是 additive，旧入站与终态发送记录按可恢复信息回填；旧昵称未保存的无法保证补齐。

## 发送

`POST /v1/messages/send` / `qq_send_message`：

```json
{
  "idempotency_key": "reply-123-v1",
  "reply_to": 123,
  "text": "这是视频",
  "media_url": "https://cdn.example.com/example.mp4",
  "media_kind": "video",
  "filename": "example.mp4",
  "markdown": false
}
```

参数：

- idempotency_key 必填：标识同一次发送动作，最多128字符。
- 被动回复填 reply_to；主动发送填 scene、chat_id。
- text 最多2000字符，可纯文字、纯媒体或媒体附文字。
- 媒体来源二选一：media_url（公网HTTPS文件直链）或 media_id（上传结果），同时指定 media_kind=image/voice/video/file。
- filename 为可选文件名元数据。
- quote_to 或 quote_key 用于 QQ 引用框，分别指定收到的消息 seq 或已发送动作 key。
- Markdown 用于 C2C/group 文字。媒体一次一份，C2C/group 支持。

### QQ 引用消息

接收到用户的引用回复时，lite 消息增加 `quote`，只在有明确引用标记时输出：

```json
{
  "seq":42,
  "text":"这个方案怎么样？",
  "from":{"id":"u1","name":"小明"},
  "quote":{
    "seq":8,
    "from":{"id":"u2","name":"小红"},
    "text":"周末去爬山",
    "attachments":[{"kind":"image","url":"https://cdn.example.com/a.jpg"}]
  }
}
```

`quote.seq` 对应已经接收的原消息，`quote.key` 对应机器人此前发送的消息，匹配到本地索引时才出现。引用内容由官方事件快照提供，包含作者、文本、附件和嵌套内容；快照尚未提供时标记 `available:false`。普通的上下文消息元素与显式引用分开处理。

发送引用框使用 `quote_to` 或 `quote_key`：

```json
{"idempotency_key":"quote-42-v1","reply_to":42,"quote_to":8,"text":"我针对这句话回答。"}
```

```json
{"idempotency_key":"quote-own-v1","scene":"group","chat_id":"群OpenID","quote_key":"之前成功发送的key","text":"补充我刚才的回答。"}
```

- `reply_to`：使用哪条收到的消息的被动回复额度与路由。
- `quote_to`：在 QQ 显示引用框，引用某条收到的消息（填入站 seq）。
- `quote_key`：在 QQ 显示引用框，引用机器人成功发送的消息（填该动作 key）。
- 两个引用参数选其一。当前在 C2C/group 支持文字、Markdown 和媒体的引用发送。
- 桥内部把入站的 `message_scene.ext.msg_idx` 和出站的 `ext_info.ref_idx` 转成官方 `message_reference.message_id`。调用方只提交 seq/key。
- 原始 `message_id`（ROBOT1.0）与引用索引不同；旧记录当时未保存入站 msg_idx 时会返回“引用索引不可用”，收到一条新消息即可测试新版引用链路。
- 引用目标来自当前会话，回复资格和引用展示独立。主动引用依旧走当前主动消息路径，实际展示和有效性以 QQ 平台结果为准。

### URL

桥校验 HTTPS 地址和公网 DNS，在提交及上传前检查，通过官方 MediaUploader URL 路径让 QQ 平台下载转存，再发送 msg_type=7。参数 `srv_send_msg=false` 保持上传与发送独立。`BRIDGE_MEDIA_URL_HOSTS` 可进一步限定精确媒体域名。链接可能过期或平台无法访问，应查看阶段与平台结果。桥不提供本地下载/任意请求代理。

### 上传

HTTP `POST /v1/media` multipart file，返回 media_id/size/expires_at；默认文件20MiB，总请求21MiB，总保留256MiB、TTL24h。

MCP `qq_upload_media(filename,content_base64)` 标准Base64，解码最多4MiB。较大本地文件用HTTP multipart；已有直链直接使用 media_url。

### 主动联系

`BRIDGE_PROACTIVE_SESSIONS=c2c:openid,group:group_openid` 为部署者的精确会话授权。QQ 客户端也需允许主动发送。Agent 的 heartbeat 决定何时调用，例如：

```json
{"idempotency_key":"reminder-001","scene":"c2c","chat_id":"openid","text":"提醒内容"}
```

### 结果

提交新动作 HTTP202，重复同内容key返回原记录；同key不同内容409。`GET /v1/sends/{key}` / qq_get_send_result 返回 status、stage、msg_seq、error_details、result 等。

| status | 含义 |
|---|---|
| queued | 入队 |
| sending | 正在执行 |
| sent | 平台受理 |
| failed | 可确定未发送或平台明确拒绝 |
| unknown | 发送结果有歧义，需要核实 |

阶段包含 validate/upload/send/sent 等；error_details 保留数字 HTTP 状态与官方业务码，`error` 给出对应含义（例如 850026 表示 QQ 无法下载该 URL、850019 表示格式不支持、850031 表示超出大小）。上传阶段失败可确定尚未调用发消息，post 阶段超时等保留 unknown。同一业务动作沿用原 key 查询；unknown 不自动重发。

媒体 URL 由 QQ 平台抓取。若平台报告下载失败（850026/850027 等），桥会按官方建议自行下载一次并改用分片上传重试；两者都失败时 `error` 会同时说明。

## 回复规则与兼容

默认C2C3600秒/4次，群300秒/5次；频道/频道私信300秒，桥端预留次数上限5。平台实际规则可能收紧。可用BRIDGE_REPLY_WINDOW_<SCENE>、BRIDGE_MAX_REPLIES_<SCENE>配置；旧统一BRIDGE_MAX_REPLIES_PER_MESSAGE可收紧全部场景。能力接口返回生效值。每条原消息使用持久化预留msg_seq，重启保留。

默认采用精确会话授权，媒体URL校验独立于Host/Origin；`BRIDGE_ALLOWED_HOSTS` / `BRIDGE_ALLOWED_ORIGINS` 用于MCP传输域名校验。403/409/413/422对应权限、冲突/时效、大小、参数问题。收到的文字与附件由宿主按用户授权使用；桥凭据通过私密配置传递。

## 数据库存储

环境变量 `BRIDGE_DATABASE_URL` 配置 PostgreSQL URI（`postgres://` 或 `postgresql://`），留空时使用 `BRIDGE_DATA_DIR/bridge.sqlite3`。服务自动建表，保持 MCP 与 REST 的消息编号、游标和幂等 key 语义。

`qq_status` / `GET /v1/status` 新增：

```json
{"storage":{"backend":"postgresql","active":true}}
```

滚动部署等待接管时 `active:false`，数据接口返回 503；`GET /health` 为存活检查，正常返回 200。数据库连接故障的数据请求返回脱敏 503。平台环境面板保存真实 URI，日志与状态响应只报告后端名称。

持久化范围：消息及 ack、收发历史、发送队列和幂等状态、资料/群缓存、引用索引、网关恢复状态、上传元数据。上传文件字节仍使用本地目录，`media_url` 可独立于本地上传缓存使用。后端切换不自动搬迁历史数据。
