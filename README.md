# QQ 官方机器人 MCP + HTTP 云桥

把 QQ 官方机器人连接到你自己的 Agent。云端单容器保持 WebSocket 连接，SQLite 保存消息、成员资料和发送记录；Agent 使用远程 MCP 或 HTTP 获取消息、浏览对话、发送文字和富媒体。

## 功能

- **媒体直链发送**：给 MCP 传 `media_url` + `media_kind`，QQ 平台下载转存并发送图片、MP4、语音或文件。
- **文件上传发送**：本地文件可用 HTTP multipart（默认 20MiB），小文件可用 MCP Base64（4MiB）。
- **发言人资料**：自动保存官方事件中的昵称、角色、头像等可用字段，按会话维护资料缓存。
- **主动联系**：配置会话授权后，Agent 可主动给用户/群发送消息。
- **回复状态**：每条消息提供回复截止时间、剩余时间、剩余次数和主动联系授权状态。
- **双向历史**：同时浏览收到的消息和机器人成功发送的内容。
- **发送诊断**：查看排队、上传、发送阶段与最终结果。
- 同端口 MCP `/mcp`、HTTP `/v1/*`、存活检查 `/health`；保留可分发 Skill。

## 部署

详细步骤见 [cloady 部署](docs/DEPLOY_CLOADY.md)，升级步骤见 [本轮升级](docs/UPGRADE.md)。

1. 在 QQ 官方平台创建机器人，取得 AppID/AppSecret，接收方式选择 WebSocket。
2. 将仓库部署至 cloady，Compose 文件为根目录 `docker-compose.yml`。单服务 `bridge`，端口 8080，单副本。
3. 在平台私密环境配置 `QQBOT_APP_ID`、`QQBOT_APP_SECRET`、`BRIDGE_API_TOKEN`，将实际 HTTPS 域名加入 `BRIDGE_ALLOWED_HOSTS`。
4. 命名卷 `qqbot-data` 挂载 `/data`，保留旧卷进行升级。容器 UID/GID 10001，卷需要该用户可写。
5. 浏览 `/health`，接入 MCP 后调用 `qq_status` 查看 QQ 状态。

镜像运行 `uvicorn bridge.main:app --host 0.0.0.0 --port 8080 --workers 1`。通过平台 HTTPS 域名访问；若自己使用 Compose，端口映射所有网卡，应由 HTTPS 代理/防火墙控制公网访问。

## Agent 接入

```text
类型：Streamable HTTP
URL：https://你的域名/mcp
请求头：Authorization: Bearer <BRIDGE_API_TOKEN>
```

`/mcp` 与 `/mcp/` 都可直接使用。MCP 使用官方 Python SDK `mcp==1.30.0`，QQ SDK 固定 `qqbot-agent-sdk==1.2.2`。

| 工具 | 用途 |
|---|---|
| qq_status | 在线状态和队列诊断 |
| qq_get_capabilities | 媒体能力、限额、回复规则及主动配置 |
| qq_get_messages | 游标读取新消息，附发言人资料和回复状态 |
| qq_ack_message | 标记消息已处理 |
| qq_list_sessions | 浏览会话、名称与主动联系状态 |
| qq_get_session_messages | 浏览会话收到的消息 |
| qq_get_session_history | 浏览收发双向历史 |
| qq_get_profiles | 按会话筛选成员资料 |
| qq_get_profile | 查询某位发言人的缓存资料 |
| qq_send_message | 文字、媒体 URL 或已上传文件发送 |
| qq_get_send_result | 查询发送阶段与结果 |
| qq_upload_media | 上传小文件，取得 media_id |

MCP description 采用简洁的正向能力说明。完整接口见 [docs/API.md](docs/API.md)。

### URL 媒体回复

```json
{
  "idempotency_key": "video-reply-123-v1",
  "reply_to": 123,
  "text": "这是视频",
  "media_url": "https://cdn.example.com/example.mp4",
  "media_kind": "video",
  "filename": "example.mp4"
}
```

图片 `image`、视频 `video`、语音 `voice`、文件 `file`。一次一份媒体，可附文字。QQ 平台从公网 HTTPS 直链下载转存；模型只提交 URL。URL 应是文件本身而非网页/网盘预览页。桥只校验地址及公网 DNS，媒体内容由 QQ 平台获取；若启用本地下载回退，应另外实现完整重定向和连接 IP 校验。

### 主动联系

设置精确会话：

```dotenv
BRIDGE_PROACTIVE_SESSIONS=c2c:用户OpenID,group:群OpenID
```

调用示例：

```json
{"idempotency_key":"reminder-unique-v1","scene":"c2c","chat_id":"用户OpenID","text":"提醒内容"}
```

Agent 宿主的 heartbeat 负责唤醒 Agent，由 Agent 判断发言时机并调用工具。服务器授权、QQ 客户端允许主动发送、官方实际权限/频率共同决定结果。主动消息默认只向显式配置的会话开放。

### 昵称与资料

桥自动保存 `author.username` 等官方提供的字段，并使用稳定 ID 区分同名用户。消息中的 sender 是当时的资料快照，缓存资料供后续查询。QQ 号码只有来源明确时才作为独立资料处理；OpenID、union_openid、union_user_account 都不自动解释成 QQ 号。独立查询群成员接口属于官方内邀能力，基础版本从事件自动积累资料。

## 回复规则

| 场景 | 默认有效期 | 每条原消息默认可预留回复数 |
|---|---|---|
| C2C 私聊 | 3600 秒 | 4 |
| 群聊 | 300 秒 | 5 |
| 频道/频道私信 | 300 秒 | 桥端保护上限 5 |

C2C 与群聊数值依据当前官方[消息收发概述](https://bot.q.qq.com/wiki/develop/api-v2/server-inter/message/overview.html)。频道 5 次是桥端策略，不是官方承诺。配置可以收紧，最终以平台实际受理为准。原始 timestamp 用于计算窗口，队列及上传后会再次核对。旧消息升级后按当前规则计算回复状态。

## 数据与运行约定

- `/v1/messages` 和旧会话 messages API 的 seq 始终对应入站消息；双向 history 使用独立 `history_seq`，分别保存游标。
- 发送动作使用稳定 idempotency_key。`queued` 为入队，`sent` 为平台受理，`unknown` 需核实后决定后续动作。同一动作沿用同一 key 查询结果。
- SQLite 以 additive 迁移保留旧消息、发送、上传及网关 session；迁移前建议平台卷快照/SQLite backup。单副本、单 worker。
- 已提交归档随卷保留；平台休眠或网关断线期间事件恢复取决于官方。资料和旧历史的字段丰富度取决于当时实际保存的数据。
- 主动权限与 QQ 服务范围是独立配置；单聊与群成员 OpenID 按各场景处理。
- Bearer token 保存在平台 Secret/Agent 私密配置。签名媒体 URL 可能包含访问令牌，日志与错误仅保留脱敏诊断。
- 单机器人、共享密钥架构适合互信 Agent；多租户需要独立鉴权与数据隔离。

## 配置与测试

`.env.example` 列出部署参数。数值可选项对空值/空白使用默认值，配置错误报告变量名。上传文件默认 20MiB、总请求 21MiB、本地媒体总保留 256MiB、TTL 24h；MCP 内联上传 4MiB。`BRIDGE_MEDIA_URL_HOSTS` 可限制为逗号分隔的精确媒体域名，空值表示校验公网 HTTPS 地址。

```powershell
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements-dev.txt
.venv\Scripts\python -m pytest -q
```

测试注入 fake QQ 后端并包含官方 MCP 客户端 loopback 测试。真实 QQ 媒体/群昵称/主动发送在云端升级后验收，见 [docs/VALIDATION.md](docs/VALIDATION.md)。

Skill 保留在 `skills/qq-official-bridge/`，分发包为 `dist/qq-official-bridge.skill`。
