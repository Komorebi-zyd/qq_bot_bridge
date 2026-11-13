# HTTP API v1（客户端协作契约）

除 `GET /health` 外，所有 `/v1` 请求必须带 `Authorization: Bearer <BRIDGE_API_TOKEN>`。HTTP 8080，单实例。时间为 UTC ISO8601。错误为 JSON `detail`。禁止提交本地路径或远程 URL。

* `GET /health`：进程活着即 200，不代表 QQ 在线。
* `GET /v1/status`：网关 `connected`, `api_ready`, `reconnecting` 及队列统计。HTTP 200 不代表发送一定成功。
* `GET /v1/messages?after=0&limit=50&scene=group&chat_id=...`：`{items:[{seq,message_id,scene,chat_id,user_id,content,attachments,received_at,timestamp,reply_deadline,acked,...}],next_cursor}`；scene/chat_id 可选；limit 1..200。seq 单调递增，游标排他。空结果 next_cursor 等于 after；客户端仅在可靠处理后保存游标。ack 不影响此接口，历史不会删除。
* `POST /v1/messages/{seq}/ack`：幂等标记消费 `{seq,acked:true}`，不删除消息。
* `GET /v1/sessions`：`{items:[{scene,chat_id,last_seq,message_count}]}`，仅实际观察到的会话，不是联系人目录。
* `GET /v1/sessions/{scene}/{chat_id}/messages?after=0&limit=50`：与消息分页相同。
* `POST /v1/media`：multipart 字段 `file`；返回 `{media_id,size,expires_at}`。仅上传字节，不支持 URL/path。上传文件名不作为存储路径。文件默认最大 20MiB，请求总大小默认 21MiB。
* `POST /v1/messages/send`：JSON `{idempotency_key,reply_to?:整数seq,scene?:c2c|group|guild|dm,chat_id?:字符串,text:"",media_id?:字符串,media_kind?:image|voice|video|file,markdown:false}`。每次至多一份媒体；可文字+媒体。回复路由从原消息派生，不能覆盖成别的会话；主动发送必须在服务端授权会话列表。media_kind 和 media_id 必须同时存在；媒体仅支持 c2c/group。返回发送记录（HTTP 202，新建；重复键返回同一记录）。同键不同正文 409。不可用/过期原消息 409，未授权主动会话 403。markdown 与媒体不可组合，且仅 c2c/group 支持。默认每条原消息只允许预留一次回复（failed/unknown 也不释放配额），多回复需要服务端显式配置。
* `GET /v1/sends/{idempotency_key}`：`{idempotency_key,status,created_at,updated_at,error,result}`。status 为 queued/sending/sent/failed/unknown。发送异步，queued 不等于送达。sending 时进程重启或网络结果不确定变 unknown，绝不自动重发；客户端也不得换键盲重试。failed 是本地验证失败或明确平台拒绝。sent 表示平台接受，不保证用户阅读。幂等记录持久化，无自动过期。

附件包括平台提供的 filename/content_type/url/size/voice_wav_url（若有）；原消息保留平台 `asr_text`。服务端不会访问这些 URL。回复期限按原消息 timestamp 计算，并以桥接收时间加窗口作为上限；未知/无效或明显未来 timestamp 不允许被动回复。默认窗口各场景 240 秒，实际平台权限/配额可能更严格。

ack、发送和客户端游标是独立状态。建议先可靠保存收到的消息，再 ack；轮询发送状态。409/403/413/422 不应自动重试。HTTP 中断后用原幂等键查状态。

## MCP Streamable HTTP（同端口、同权限）

`/mcp` 与 `/mcp/` 无重定向；使用官方 SDK `mcp==1.30.0`，stateless + JSON response。客户端先 initialize，再 tools/list / tools/call；Accept 须兼容 `application/json, text/event-stream`。GET 在无状态模式返回 405，不是鉴权豁免。所有 HTTP 方法都要求相同 Bearer header；仅 `/health` 公共。不得在查询字符串放 token。无状态模式不需要持久 MCP session ID，重启后消息/游标/幂等仍由 SQLite 和客户端保存。

| 工具 | 参数（未注明者可选） | 对应 REST |
|---|---|---|
| qq_status | 无 | GET /v1/status |
| qq_get_messages | after=0, limit=50, scene, chat_id | GET /v1/messages |
| qq_ack_message | seq 必填 | POST /v1/messages/{seq}/ack |
| qq_list_sessions | 无 | GET /v1/sessions |
| qq_get_session_messages | scene、chat_id 必填；after=0, limit=50 | GET /v1/sessions/{scene}/{chat_id}/messages |
| qq_send_message | idempotency_key 必填；text="", reply_to, scene, chat_id, media_id, media_kind, markdown=false | POST /v1/messages/send |
| qq_get_send_result | key 必填 | GET /v1/sends/{key} |
| qq_upload_media | filename、content_base64 必填 | POST /v1/media |

成功工具结果为相同领域 JSON（SDK structuredContent，并有文本兼容内容）；领域错误为 MCP `isError=true`，描述包含 HTTP 状态与安全的原因，不回显验证输入。传输级错误直接 HTTP 401/413/421/403 等。seq 是桥归档序号，不是 QQ msg_id；after >=0，limit 1..200。scene 为 c2c/group/guild/dm，media_kind 为 image/voice/video/file；发送其他校验和幂等语义完全同 REST。MCP 不新增队列或发送重试。

上传只接受标准 base64 字节，不接受 URL/path，解码上限 4194304 字节，编码长度预检查后再解码；空文件拒绝。MCP 总请求默认 22020096 字节，包括 chunked；文件/媒体空间限制仍由 REST 执行。大文件直接 REST multipart，再把 media_id 传给 qq_send_message。不要记录请求正文/base64。

`BRIDGE_ALLOWED_HOSTS` 是逗号分隔的精确 Host 值（包括实际端口），默认 localhost:8080、127.0.0.1:8080、[::1]:8080。云端域名需显式加入。`BRIDGE_ALLOWED_ORIGINS` 为精确 Origin 列表（scheme+host+port），无 Origin 可访问；不接受通配符。SDK DNS rebinding 防护始终启用。反代须保留或匹配转发 Host，不以关闭防护解决 421。

MCP 不会唤醒 Agent：Agent 需自设心跳/定时调用 qq_get_messages 并持久化游标；收到的是官方网关已经推送并归档的数据，不是官方任意聊天历史接口。附件 URL/voice_wav_url 原样保留，视为不可信外部输入，桥不自动抓取。现有 skills/ 和 REST 是保留的兼容路径。
