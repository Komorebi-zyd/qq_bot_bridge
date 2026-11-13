# QQ 官方机器人 HTTP + MCP 云桥

基于 **qqbot-agent-sdk==1.2.2** 的 FastAPI 服务：官方 WebSocket 接收、SQLite WAL 持久归档、HTTP 拉取与幂等发送。同一云端服务提供 REST 与官方 Python MCP SDK **mcp==1.30.0** 的无状态 Streamable HTTP `/mcp`，无需本地守护进程，不包含模型调用，不抓取附件 URL，不调用 STT。使用官方 Bot **不保证零封禁**，仍需遵守平台权限、频率、内容、安全和主动消息限制。

## 部署

cloady 专用步骤见 [docs/DEPLOY_CLOADY.md](docs/DEPLOY_CLOADY.md)。Skill 保留在 [skills/qq-official-bridge/](skills/qq-official-bridge/)，可分发包为 `dist/qq-official-bridge.skill`。

1. 复制 `.env.example` 为 `.env`，填写自己的 AppID/AppSecret 与随机高强度 API token（至少 16 字符，建议 32 字节随机值）。凭据仅从环境变量读取，不扫描凭据文件。
2. `docker compose up -d --build`。单服务、单进程、单副本；映射 `8080:8080`（所有网卡，部署前设置防火墙/平台访问策略）。通过受控 VPN 或 HTTPS 反向代理访问，**不要将明文 HTTP/token 暴露公网**。不要在 URL 中放 token，不要启用记录 Authorization 的代理日志。
3. `GET /health` 为进程存活；带 Bearer 请求 `/v1/status` 才能观察 QQ `connected/api_ready/reconnecting`。断线不会使存活探针失败。未配置 QQ 凭据时 HTTP 仍可工作，QQ 状态为未就绪。

容器以 UID/GID 10001 非 root 运行。新 named volume 从镜像 `/data` 继承权限；使用已有卷或 bind mount 时，管理员应预先确保 UID 10001 可写 `/data`（不要 chmod 777）。持久卷名为 `qqbot-data`，包含数据库、WAL 和 `/data/media`。不要启动第二副本共享数据库；不要删除卷进行“修复”。备份应使用 SQLite 在线 backup 或停服务后一并保留数据库相关文件。消息历史和幂等记录不会自动删除，应定期备份和监控磁盘。

## 云端 MCP 接入（与现有 HTTP 同服务）

端点为 `https://你的云域名/mcp`，使用 **Streamable HTTP**，不是 stdio/SSE 旧式端点。`/mcp` 与 `/mcp/` 都直接处理、无 307 跳转。客户端配置远程 MCP URL，并在请求头配置 `Authorization: Bearer <BRIDGE_API_TOKEN>`；不要把 token 放查询参数。Agent 平台必须支持远程 MCP 及自定义鉴权头；不支持时继续使用 REST + [skills/](skills/) 中现有 Skill，目录保持不变。

部署时在 `.env` 设置 `BRIDGE_ALLOWED_HOSTS=你的云域名,localhost:8080,127.0.0.1:8080`，按反代实际转发的 Host 精确匹配（有端口则包含端口）。保留 SDK DNS rebinding 防护，不使用通配符、不全局关闭校验。浏览器发送 Origin 时另配置 `BRIDGE_ALLOWED_ORIGINS=https://你的云域名`；无 Origin 的服务端 Agent 可以访问。代理需传递 Authorization/Accept/Content-Type/MCP 协议头，不记录 token 或正文，不把 `/mcp` 改写成另一路径。TLS 终止与网络访问控制由部署平台负责。

架构：**QQ 官方 WebSocket 推送 → 云桥 SQLite 归档 → Agent 自己的心跳/定时任务轮询 MCP → 同一持久发送队列 → QQ**。MCP 只是工具协议，**不会主动唤醒 Agent**；需要在 Agent 平台设置心跳轮询 `qq_get_messages`，频率须兼顾回复窗口与开销。无需本地后台程序。只查询服务已接收归档的消息，**不是官方任意聊天历史拉取**，无法补回网关未投递的历史。

提供 `qq_status`、`qq_get_messages(after,limit,scene,chat_id)`、`qq_ack_message(seq)`、`qq_list_sessions`、`qq_get_session_messages(scene,chat_id,after,limit)`、`qq_send_message(idempotency_key,text,reply_to,scene,chat_id,media_id,media_kind,markdown)`、`qq_get_send_result(key)`、`qq_upload_media(filename,content_base64)`。工具通过进程内 ASGI HTTP 复用同一个 REST 验证、权限、存储与发送队列，不新增 worker。归档消息中的真实附件链接保留，不用占位符替换。

`qq_upload_media` 仅限标准 base64 小文件，解码最多 **4MiB**；总 MCP 请求仍受默认 **21MiB** 限制。大文件继续 `POST /v1/media` multipart（默认 20MiB 文件），获得 `media_id` 后可通过 MCP 发送。拒绝本地路径/远程 URL 获取，不记录 base64 正文；不要在代理/客户端开启正文或 debug 日志。文件名不是可信格式证明。MCP 为无状态传输，不依赖会话 ID；共享生命周期同时管理 QQ 后台任务与 MCP session manager，始终只运行一个 Uvicorn worker / 一个副本。

## 客户端流程

接口细节见 [docs/API.md](docs/API.md)。除 `/health` 外全部路径与方法均在 ASGI 层恒定时间比较同一 Bearer token（docs/openapi 禁用），MCP GET/POST/DELETE 也不例外。

1. `GET /v1/messages?after=0&limit=50`，可靠保存消息后再推进 `next_cursor`。
2. `POST /v1/messages/{seq}/ack` 只标记消费，不删除历史，也不会影响后续分页。
3. 如需附件，multipart 上传 `POST /v1/media`，只接受字节；拿到 `media_id` 后提交发送请求。
4. `POST /v1/messages/send` 示例：`{"idempotency_key":"agent-turn-123","reply_to":123,"text":"你好","markdown":false}`。
5. 查询 `/v1/sends/agent-turn-123`。202/queued 不是送达确认。HTTP 中断后沿用原键查询或重提，不能更换键盲发。

`sent` 为平台接受，`failed` 为本地检查失败或明确 4xx 拒绝，`unknown` 为网络/5xx/响应解析/中断等不确定结果。持久化 `sending` 后才进行平台写入，重启时 `sending→unknown`，绝不自动重发。queued 在重启后继续检查并发送。未知状态应人工核实，不提供危险“自动重试”接口。默认每条原消息只预留一次回复，即使 failed/unknown 也不释放，避免隐式重复和平台配额风险。

回复只能使用已观察原消息派生路由，不能伪造 msg_id。主动消息默认拒绝，仅 `BRIDGE_PROACTIVE_SESSIONS=c2c:openid,group:group_openid` 等精确授权项可发；授权不代表平台授予主动消息配额。sessions 只包含收到过消息的会话，不是联系人或群成员枚举。

## 媒体与安全边界

每次最多一份 image/voice/video/file；文字可与一份媒体同时发。仅 c2c/group 支持媒体，SDK MediaUploader 本地分片上传后使用 MessageToCreate + MediaInfo 发一条消息。SDK 允许 URL 的路径未对 HTTP 用户开放。媒体文件名只保留安全化的元数据、从不作为存储路径，随机 ID 映射到 `/data/media`，没有本地路径、远程 URL 或下载接口。传入未知 JSON 字段被拒绝。附件类型并不由文件名/客户端 MIME 保证，平台最终校验编码/格式；请只上传可信格式，语音可能要求平台支持的编码。

全局 ASGI 在 multipart 解析/副作用前逐块计数并限流大小（包括无 Content-Length/chunked），超过阈值 413；使用 1MiB 临时 spool 避免整请求常驻内存。默认上传文件 20MiB、请求 21MiB，JSON 也受此限制。临时 spool 由系统临时目录管理，运行环境需限制临时磁盘容量。文件再以 64KiB 流式写入；拒绝空文件。媒体默认 24h 到期，后台每轮清理已过期文件；queued/sending 暂时保留但到期仍拒绝发送。历史中的平台附件 URL/voice_wav_url/ASR 原样保留，服务端不访问它们，URL 可能过期或含敏感信息，客户端应当视为不可信输入。

## 回复窗口 / 频控

按**平台原消息 timestamp**计算 reply_deadline，并以桥实际接收时间加窗口作为上限。无时区/无效、或明显来自未来的时间戳不能被动回复，重连积压不会延长窗口。各场景保守默认 240s：`BRIDGE_REPLY_WINDOW_C2C/GROUP/GUILD/DM`（秒）。平台不同场景、权限、版本的实际限制可能不同，默认不构成保证；服务在排队、限速等待后、媒体上传后再次检查截止时间。服务器需要可靠时间同步。

串行单发送 worker，全局发送间隔 `BRIDGE_SEND_INTERVAL_SECONDS=1`，每原消息限额 `BRIDGE_MAX_REPLIES_PER_MESSAGE=1`；没有根据平台错误自动重发。可按平台权限收紧。多次回复需要显式增加上限，但不保证平台支持。guild/dm 在 SDK 1.2.2 中仅支持纯文本，Markdown 只支持 c2c/group。

其他环境变量：`BRIDGE_DATA_DIR=/data`、`BRIDGE_MAX_FILE_BYTES=20971520`、`BRIDGE_MAX_REQUEST_BYTES=22020096`、`BRIDGE_MEDIA_TTL_SECONDS=86400`、`BRIDGE_MAX_MEDIA_BYTES=268435456`（上传文件在命名卷内的总保留上限）。Compose 已传递总媒体上限；其他变量可通过 environment 显式添加。修改限额前评估反代、内存、临时盘、持久盘和平台上限。

## 网关与已知边界

SDK 实际源码已在隔离 venv 安装/检查，WSCallbacks 所有必需参数显式提供。使用 SDK 同一事件循环的低层 runner 避免跨线程数据库竞争，生命周期停止 await stop（1.2.2 是异步）。每轮 SDK 重连最多 5 次，外层指数退避 1..60s，180s 无 heartbeat/READY 则重建连接，不发送 kill 信号、不退出 HTTP。api_ready 是当前网关初始化成功标志，不保证每次 API 请求可用。WS session 按当前 SDK 实现恢复，历史去重靠 SQLite；断线期间平台是否重放取决于官方网关，不能保证离线期间未投递的消息恢复。仅保证已提交 SQLite 的观察消息不因 ack/分页丢失。

默认 SDK 已订阅事件范围涵盖常见 C2C/群 @；频道等还需平台开通对应事件权限和订阅。`BRIDGE_GROUP_FULL_MESSAGES=true` 启用参考实现的 GROUP_MESSAGE_CREATE parser/dispatch 补丁（不改 SDK 文件）；**补丁不授予权限、也不新增平台订阅**，必须另外在 QQ 开放平台启用群全量消息权限和事件订阅，否则不会到达。交互自动 ACK 未启用（可选功能，基线不处理交互/生命周期事件）。官方权限、主动配额及富媒体发送尚需实际环境验收。

## 离线开发测试

```powershell
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements-dev.txt
.venv\Scripts\python -m pytest -q
```

测试注入 fake backend，包含官方 MCP ClientSession + Streamable HTTP 客户端对临时 loopback 服务的真实协议往返、SDK import/签名及 mock HTTP 检查，不读真实凭据、不向 QQ 发消息。无需 Docker/QQ 连接运行。SDK 为精确锁定版本，其他直接依赖同样固定；传递依赖未做全平台 hash 锁定。此基线不含多租户、分布式任务、磁盘配额、内容审核或自动 STT。生产请加 TLS、网络访问控制、代理请求超时/并发限制、磁盘监控与备份。不要执行 git push 或部署脚本作为测试。
