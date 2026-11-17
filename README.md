# QQ 官方机器人 MCP 云桥

**让任何 Agent 住进 QQ。**

一个容器，同时暴露远程 MCP 和 HTTP API。云端常驻官方 WebSocket，把你的 Agent 变成真正能在 QQ 私聊和群聊里说话的机器人——收发文字、图片、视频、语音、文件，知道谁在说话、在哪个群、还能不能回复。

![Python](https://img.shields.io/badge/Python-3.12-3776AB?logo=python&logoColor=white)
![MCP](https://img.shields.io/badge/MCP-Streamable%20HTTP-6E56CF)
![Tools](https://img.shields.io/badge/tools-12-2ea44f)
![Tests](https://img.shields.io/badge/tests-236%20passed-2ea44f)
![Deploy](https://img.shields.io/badge/deploy-single%20container-0b7285)
![Official](https://img.shields.io/badge/route-official%20Bot%20API-e8590c)

```text
QQ 用户  ←→  QQ 开放平台  ←→  这个容器  ←→  你的 Agent
            官方 WebSocket      MCP / HTTP      心跳自己定
```

本地不用跑任何守护进程。Agent 只要有心跳，就能主动巡查、主动开口。

---

## 为什么不是又一个 Bot 框架

| | 常见做法 | 这个项目 |
|---|---|---|
| 协议 | 逆向客户端协议 | 腾讯官方 Bot API v2 |
| Agent 位置 | 塞进 Bot 进程里 | 独立进程，通过 MCP 调用 |
| 本地负担 | 常驻脚本 + 掉线重连 | 一个容器，云端常驻 |
| 多端复用 | 每个 Bot 一套代码 | 一个 MCP 端点，接几个 Agent 都行 |
| 媒体发送 | 自己拼 Base64 | 给个 URL，平台自己取 |
| 失败排查 | 一个 400，全靠猜 | 官方业务码 + 阶段定位 |

---

## 能力

| 能力 | 说明 |
|---|---|
| 私聊 / 群聊收发 | 文字、图片、视频、语音、文件 |
| 主动联系 | 不用等你先说话，Agent 可以主动找你 |
| 认识人 | 昵称、头像、群内角色；同名靠 ID 区分 |
| 认识群 | 群名称、群人数、群成员列表 |
| 双向历史 | 谁说过什么，双向可查，游标独立 |
| 引用对话 | 读懂被引用的正文和附件，也能引用用户或机器人之前的消息 |
| 媒体直链 | `media_url` 一句话发视频，不用 Base64 |
| 自动回退 | 平台抓不到链接，桥自己下载后分片上传 |
| 精确诊断 | 850026 是下载失败，850019 是格式不支持 |
| 省流返回 | 单条消息 378 字符，是原始结构的 24% |

表情和 @ 会被翻译成人话：`<faceType=1,...>` → `[表情:大怨种]`，`<@9A30...>` → `[@Coconut]`。

---

## 30 秒开始

**1. 建机器人**

到 [QQ 开放平台](https://q.qq.com/#/apps) 创建机器人，拿 AppID 和 AppSecret，事件接收方式选 **WebSocket**。

**2. 部署**

```bash
git clone https://github.com/Komorebi-zyd/qq_bot_bridge
cd qq_bot_bridge
cp .env.example .env    # 填 AppID / AppSecret / BRIDGE_API_TOKEN
docker compose up -d --build
```

单服务、单容器、单副本。持久数据在命名卷 `qqbot-data`。

**3. 接入 Agent**

```text
类型：Streamable HTTP
URL：https://你的域名/mcp
请求头：Authorization: Bearer <BRIDGE_API_TOKEN>
```

刷新工具列表，看到 12 个 `qq_*` 工具就通了。先调 `qq_status` 确认 QQ 在线。

详细步骤见 [cloady 部署](docs/DEPLOY_CLOADY.md)。

---

## MCP 工具

| 工具 | 用途 |
|---|---|
| `qq_status` | 在线状态、队列深度、最近诊断 |
| `qq_get_capabilities` | 可用场景、回复时限、媒体限额 |
| `qq_get_messages` | 游标拉新消息 |
| `qq_ack_message` | 标记已处理 |
| `qq_list_sessions` | 会话、群名、成员 |
| `qq_get_session_messages` | 单会话消息 |
| `qq_get_session_history` | 双向收发记录 |
| `qq_get_profiles` | 按会话筛成员资料 |
| `qq_get_profile` | 单个成员资料 |
| `qq_send_message` | 发文字 / URL 媒体 / 已上传文件 |
| `qq_get_send_result` | 查询发送阶段与结果 |
| `qq_upload_media` | 上传小文件换 media_id |

所有工具默认 `detail=lite`，需要原始字段时传 `detail=full`。

---

## 用起来是什么样

**回一句话**

```json
{
  "idempotency_key": "reply-42-v1",
  "reply_to": 42,
  "text": "收到，马上处理"
}
```

**发一段视频**

```json
{
  "idempotency_key": "video-42-v1",
  "reply_to": 42,
  "media_url": "https://cdn.example.com/demo.mp4",
  "media_kind": "video",
  "filename": "demo.mp4",
  "text": "这是你要的视频"
}
```

**主动找人**

```json
{
  "idempotency_key": "reminder-001",
  "scene": "c2c",
  "chat_id": "用户的 openid",
  "text": "该喝水了"
}
```

**带引用框回复**

```json
{
  "idempotency_key": "quote-42-v1",
  "reply_to": 42,
  "quote_to": 42,
  "text": "我针对你这句话回答"
}
```

`reply_to` 选择被动回复额度，`quote_to` 在 QQ 显示原消息引用框。引用机器人自己发过的话时，使用 `quote_key` 填此前发送的动作 key。用户引用回复时，拉回来的 `quote` 包含被引用正文、作者和附件，Agent 可以接着上下文聊。

**拉回来的消息长这样**

```json
{
  "seq": 13,
  "text": "[表情:大怨种]",
  "from": { "id": "59F5…", "name": "蝉", "role": "member", "avatar": "https://…" },
  "at": "2026-11-13T20:43:22+08:00",
  "can_reply": true,
  "replies_left": 5,
  "reply_deadline": "2026-11-13T12:48:22+00:00"
}
```

Agent 拿到就能判断：谁说的、说了什么、还来不来得及回。

---

## 几个刻意的设计

**回复额度按平台规则记账。** 私聊 60 分钟 4 次、群聊 5 分钟 5 次。每次回复占一个持久化的 `msg_seq`，重启不丢，不会因为并发把额度算超。

**发送是幂等的。** 一个业务动作一个 `idempotency_key`。超时了拿同一个 key 查结果，不会重复发。结果不确定时标 `unknown` 等你核实，不自动重发。

**媒体走平台原生 URL 上传。** 模型只提交直链，QQ 服务器自己取文件。平台取不到（850026）时，桥下载一份走分片上传再试一次——官方文档推荐的做法。

**返回只给该给的。** `sender` 和 `sender_profile` 合并成一个 `from`，`message_id` 不返回（回复用 `seq`），空字段不输出，秒级倒计时换成绝对截止时间。一条消息从 1516 字符压到 378。

**MCP 结果不重复。** 关掉结构化镜像，一份 JSON 只出现一次。

---

## 配置

**最少三个值就能跑起来：**

```dotenv
QQBOT_APP_ID=你的 AppID
QQBOT_APP_SECRET=你的 AppSecret
BRIDGE_API_TOKEN=自己生成的 32 字节随机串
```

用域名接 MCP 时再加一项，否则 `/mcp` 会返回 421：

```dotenv
BRIDGE_ALLOWED_HOSTS=你的域名
```

其余都有默认值，按需再开：

| 变量 | 必填 | 说明 |
|---|---|---|
| `QQBOT_APP_ID` / `QQBOT_APP_SECRET` | 是 | 官方机器人凭据 |
| `BRIDGE_API_TOKEN` | 是 | MCP的访问密钥 |
| `BRIDGE_DATABASE_URL` | 否 | PostgreSQL 连接串，留空使用本地 SQLite |
| `BRIDGE_ALLOWED_HOSTS` | 用域名时 | MCP 允许的 Host，精确匹配；默认只允许 localhost |
| `BRIDGE_ALLOWED_ORIGINS` | 否 | 客户端发 `Origin` 头时需要匹配 |
| `BRIDGE_PROACTIVE_DENY` | 否 | 主动发送黑名单，留空即全开 |
| `BRIDGE_MEDIA_URL_HOSTS` | 否 | 限定媒体域名，留空即公网 |
| `BRIDGE_GROUP_FULL_MESSAGES` | 否 | 群全量消息，需平台权限 |
| `BRIDGE_REPLY_WINDOW_C2C` 等 | 否 | 分场景回复窗口，默认私聊 3600 秒、群 300 秒 |
| `BRIDGE_MAX_REPLIES_C2C` 等 | 否 | 分场景回复次数，默认私聊 4 次、群 5 次 |

### 远程数据库 / Render

在平台私密环境变量里设置：

```dotenv
BRIDGE_DATABASE_URL=postgres://dbuser:YOUR_PASSWORD@db.example.com:10586/QQdb?sslmode=require
```

`postgres://` 和 `postgresql://` 均可。数据库需要预先创建，表由桥自动创建；数据库用户需要建表和读写权限。保留服务商给出的 TLS 参数，密码中的 `@`、`#`、`%` 等 URI 特殊字符使用百分号编码。

配置后，收到的消息、处理标记、发送队列与幂等记录、昵称和群资料、引用索引、网关恢复状态都写入 PostgreSQL。重新部署连接同一个数据库，继续使用原来的 `seq` 和发送 key。`qq_status.storage` 显示存储后端和当前实例是否处于活动状态。

`BRIDGE_DATABASE_URL` 留空时继续使用 `/data/bridge.sqlite3`。已填写但连接失败会返回数据库错误，配置的远程库是唯一数据源。切换存储后端会使用目标库现有数据，旧 SQLite 数据不自动导入。

Render 滚动部署期间，数据库锁保证只有一个实例接收与发送 QQ 消息，新实例等待旧实例退出后接管。`/health` 继续作为存活检查。服务使用一个副本、一个 worker，连接地址使用 PostgreSQL 直连或会话级连接池。

上传文件的**字节内容**仍在本地 `/data/media`；数据库保存的是记录。Render 临时磁盘重新部署后，旧 `media_id` 的文件需重新上传；图片、视频等优先使用 `media_url` 直链。

完整清单见 [.env.example](.env.example)。

---

## 直接用 HTTP

不想接 MCP 也能用，REST 与 MCP 共享同一套鉴权、存储和发送队列。

```bash
curl -H "Authorization: Bearer $TOKEN" https://你的域名/v1/messages?after=0&detail=lite
curl -H "Authorization: Bearer $TOKEN" https://你的域名/v1/capabilities?detail=lite
curl -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"idempotency_key":"reply-42","reply_to":42,"text":"收到"}' \
  https://你的域名/v1/messages/send
```

接口清单见 [docs/API.md](docs/API.md)。

## 本地开发

```bash
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements-dev.txt
.venv\Scripts\python -m pytest -q
```

236 项测试，覆盖 SQLite、PostgreSQL、官方 MCP 客户端 loopback 协议、旧库迁移、幂等重放、媒体回退、引用收发与滚动交接。PostgreSQL 集成测试使用独立测试库，QQ 后端使用模拟实现。

默认执行 SQLite 与驱动单元测试；在独立测试数据库上设置 `BRIDGE_TEST_POSTGRES_URL` 后执行 PostgreSQL 集成测试。测试会创建随机 schema 并写入模拟数据。GitHub Actions 已配置 PostgreSQL 17 服务运行完整测试。

---

## 边界

- 只呈现官方事件推送到桥的会话，不是个人 QQ 的全量历史。
- 身份标识是 OpenID，不是 QQ 号；官方不给可逆映射。
- 群名走官方群信息接口，走一次缓存一次。
- 平台侧的权限、配额、内容规则由 QQ 决定，桥只负责准确传达结果。

---

## Links

- [**Linux Do** **真诚、友善、团结、专业**，共建你我引以为荣之社区。](https://linux.do)
- [让你的AI也能发送表情包](https://github.com/Komorebi-yaodong/ai-memes)
- [超真实TTS免费用，注册账号使用免费模型，env中预填了三个声音~](https://github.com/Komorebi-yaodong/fishaudio-tts)

---

## License

MIT
