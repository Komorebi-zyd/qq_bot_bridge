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
| qq_get_profile | 一位发言人的缓存资料 | GET /v1/profiles/{scene}/{chat_id}/{user_id} |
| qq_send_message | 文字、URL 媒体、上传文件发送 | POST /v1/messages/send |
| qq_get_send_result | 发送阶段与结果 | GET /v1/sends/{key} |
| qq_upload_media | 标准 Base64 小文件上传 | POST /v1/media multipart |

MCP description 聚焦功能与参数。以下约定解释状态、权限与游标。工具响应提供 structuredContent 和文本兼容内容。

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
- Markdown 用于 C2C/group 文字。媒体一次一份，C2C/group 支持。

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
