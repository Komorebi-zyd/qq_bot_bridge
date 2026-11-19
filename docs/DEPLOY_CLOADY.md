# cloady 单服务部署

本项目提供容器构建和 Compose 配置，尚未替你创建或修改云端应用。平台具体界面/免费额度以你的控制台为准。

## 构建与服务

1. 将项目放到你控制的代码仓库。不要上传 `.env`、`.venv/`、`data/`、数据库和真实凭据。
2. 在 cloady 应用中选择该仓库，部署文件选择根目录 **`docker-compose.yml`**。
3. Compose 只有一个 service `bridge`，从根目录 `Dockerfile` 构建。HTTP 容器端口 **8080**，域名入口指向此端口。没有第二个 Bot/MCP 服务。
4. 持久卷 source 为 **`qqbot-data`**，target 为 **`/data`**，已使用长格式 volume 语法，不使用相对目录绑定。
5. 实例/副本数设为 **1**，不要改成多 worker。

## 必需私密变量

在平台环境变量/Secret 面板设置，不要把真实值写进仓库或聊天：

| 变量 | 内容 |
|---|---|
| `QQBOT_APP_ID` | 官方机器人 AppID |
| `QQBOT_APP_SECRET` | 官方机器人 AppSecret |
| `BRIDGE_API_TOKEN` | 自己生成的高强度随机桥密钥，建议至少 32 字节随机值 |
| `BRIDGE_ALLOWED_HOSTS` | 实际 HTTPS 域名与反代转发的 Host，逗号分隔，精确匹配 |
| `BRIDGE_ALLOWED_ORIGINS` | 若客户端发送 Origin，添加对应的完整 HTTPS origin |

示例允许项（替换域名，不是密钥）：

```text
BRIDGE_ALLOWED_HOSTS=qq-bridge.example.com,localhost:8080,127.0.0.1:8080
BRIDGE_ALLOWED_ORIGINS=https://qq-bridge.example.com,http://localhost:8080,http://127.0.0.1:8080
```

无 Origin 的服务端 MCP 客户端也可使用；不要为了排错把 Host 防护关闭或配置通配符。若反代将 Host 改为内部名称，只添加经过确认的实际 Host。

本轮升级详细步骤见 [UPGRADE.md](UPGRADE.md)。

## 可选开关

- 群消息接收模式在 QQ 侧为目标群设置；桥通过 `GROUP_MESSAGE_CREATE` 和 `GROUP_AT_MESSAGE_CREATE` 归档群成员消息。
- `BRIDGE_PROACTIVE_DENY=`：可选主动发送排除项，使用逗号分隔的 `scene:chat_id`。留空时主动发送由 QQ 侧权限决定。
- `BRIDGE_MEDIA_URL_HOSTS=`：使用公网 HTTPS 媒体直链，可按需指定自有 CDN 域名。
- `BRIDGE_MAX_MEDIA_BYTES=268435456`：云端上传文件总保留上限 256MiB。单文件默认 20MiB；代理层可能更严格。

## 检查与接入

1. 存活检查设为 **GET `/health`**。此接口无需鉴权，不代表 QQ 已在线。
2. 带 `Authorization: Bearer <BRIDGE_API_TOKEN>` 请求 **`/v1/status`**，核对 `connected/api_ready` 与脱敏 `last_error`。
3. Agent MCP 类型选择 **Streamable HTTP / 远程 HTTP**，URL 填 **`https://你的域名/mcp`**，请求头为同一个 Bearer token。
4. 成功列出 `qq_status` 等 13 个工具后，再测试拉消息。
5. 真人从获准的 QQ 会话发送一条测试消息，Agent 调 `qq_get_messages(after=0)`，核对文本与附件 URL。
6. 实际发送测试需主人明确授权，提交后查询 `qq_get_send_result`；`queued` 不是发送成功。

平台 HTTPS 域名一般通过 443 入口转发到容器 8080；除非平台明确给出端口映射，不要自行在域名后加 `:8080`。

服务通过出站 WebSocket 连接官方网关，不要求为 QQ 配置本桥公网 webhook。仍需按官方平台要求配置应用权限/事件订阅/测试用户。

## 注意

- 本桥是单机器人、单共享 Bearer token 的私有服务，不是公开多租户 SaaS。不要把此 token 发给不互信的人；持有者能读所有归档并调用已授权发送。
- 云端持续接收事件，Agent 心跳定时读取；MCP 不会自行唤醒 Agent，不会替代个人 QQ 拉取完整历史。
- `/data` 包含聊天归档和会话恢复状态，应限制平台成员访问并备份。不要删除持久卷排错。
- 若免费平台休眠/停止容器，停止期间接收能力受影响；能否补发由官方平台决定，不能保证无丢消息。
- 本地当前未完成 Docker 构建：检测到 Docker CLI，但 Docker Desktop Linux 引擎未运行。已通过 Compose 语法校验和离线协议测试，线上网络/权限/富媒体仍须部署后验收。

## 外接 PostgreSQL（含 Render）

私密环境变量可选设置：

```dotenv
BRIDGE_DATABASE_URL=postgresql://dbuser:YOUR_PASSWORD@db.example.com:5432/qqbridge?sslmode=require
```

- `postgres://` 同样适用，使用服务商提供的 SSL 参数；特殊字符密码按 URI 编码。
- 数据库提前创建，应用自动建表。一个数据库供一台机器人桥使用，应用账号具有建表与读写权限。
- 直连或会话级池支持实例交接锁。实例数和 Uvicorn worker 数仍设为 1。
- 环境变量留空走本地 SQLite；填写后使用 PostgreSQL，连接失败报告错误。
- Render 健康检查路径是 `/health`。`qq_status.storage` 显示活动后端，滚动更新期间新实例等待接管。
- `/data/media` 的上传文件仍需本地持久卷。Render 临时磁盘可搭配 `media_url`，部署前上传的旧文件需重新上传。
- 原 SQLite 数据不会自动导入，远程库保存从启用该连接后的记录。
