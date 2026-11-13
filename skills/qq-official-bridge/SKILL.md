---
name: qq-official-bridge
description: 通过 QQ 官方机器人云桥读取已接收消息与会话历史、查看缓存的发送者资料、获取能力配置、上传文件，并发送文字或单份媒体消息（本地上传或公开 HTTPS 媒体链接）。支持私聊、群聊、频道和 DM 的已部署能力，以及由既有 Agent heartbeat 驱动的巡查与授权主动消息。
---

# QQ 官方机器人云桥

此 Skill 连接已部署的 QQ 官方机器人桥：MCP 优先，REST/一次性 CLI 备用。云桥持续接收官方事件；本地 Agent 的既有 heartbeat 会唤醒 Agent 执行巡查，CLI 不启动本地守护进程。

## 连接与工具

配置私密环境变量：`QQ_BRIDGE_URL=https://qq-bridge.example.com`（根地址，不是 `/mcp`）和 `QQ_BRIDGE_TOKEN`。不要在对话中粘贴 token。

已连接 MCP 时优先使用宿主提供的 schema。常用工具：

- `qq_status`：查看桥和 QQ 网关状态。
- `qq_get_capabilities`：发现当前部署启用的场景、发送能力、限制和有效期配置。
- `qq_get_messages`：以入站 `seq` 游标读取新收到的官方事件。
- `qq_list_sessions`、`qq_get_session_messages`：浏览桥已保存的会话和传统入站消息视图。
- `qq_get_session_history`：以独立 `history_seq` 浏览某会话的 `all`、`in` 或 `out` 历史。
- `qq_get_profiles`、`qq_get_profile`：读取事件中缓存的发送者资料快照。
- `qq_ack_message`：确认已处理的入站消息。
- `qq_upload_media`：上传明确授权的小文件；大文件可用 HTTP multipart/CLI。
- `qq_send_message`：发送文本、一份已上传媒体，或一个公开 HTTPS 直链媒体。
- `qq_get_send_result`：按幂等 key 查询实际发送结果。

CLI 同样可完成这些工作：

```text
python <Skill目录>/scripts/qq_bridge.py capabilities
python <Skill目录>/scripts/qq_bridge.py inbox --after 0 --limit 50
python <Skill目录>/scripts/qq_bridge.py history --scene c2c --chat-id <会话ID> --direction all
python <Skill目录>/scripts/qq_bridge.py profiles --scene group --chat-id <会话ID>
```

## Heartbeat 工作流

1. 先调用 `qq_status`，再用 `qq_get_capabilities` 了解当前可用功能和限制。
2. 从上次已完成处理的 `next_cursor` 调用 `qq_get_messages`。首次巡查先确认从最新开始还是处理积压。
3. 按 `scene + chat_id` 整理会话；需要上下文时使用 `qq_get_session_history`。它的 `history_seq` 与入站 `seq` 分开保存，不能混用。
4. 需要识别发送者时读取缓存 profile。消息的 `sender.username` 是当时昵称快照，`sender_profile` 与资料查询接口提供最新缓存；真实 QQ 号可能为 `null`，以明确来源为准。字段丰富度取决于官方实际推送。
5. 回复使用原消息 `seq` 作为 `reply_to`；每个业务动作生成稳定的 `idempotency_key`。超时后先查同一 key 的结果；`unknown` 时保留待核实，避免重复发送。
6. 处理完成或可靠记入待办后再 ack，并推进 `next_cursor`。ACK 不删除历史。

默认被动回复窗口为：C2C 3600 秒（4 次回复）、群 300 秒（5 次回复）、频道/DM 300 秒（5 次回复）。部署可配置更低限制；以 `qq_get_capabilities` 返回值和实际平台结果为准。

## 发送媒体

文本回复：

```text
python <Skill目录>/scripts/qq_bridge.py send --reply-to 123 --text "收到，谢谢" --key reply-123-v1
```

公开 HTTPS 直链让 QQ 原生获取媒体，无需 Base64：

```text
python <Skill目录>/scripts/qq_bridge.py send --reply-to 123 --media-url "https://media.example.com/picture.png" --kind image --filename picture.png --text "图片已收到" --key image-123-v1
```

本地文件先以实际字节 multipart 上传，再发送返回的 `media_id`：

```text
python <Skill目录>/scripts/qq_bridge.py upload --file "E:/Pictures/example.png"
python <Skill目录>/scripts/qq_bridge.py send --reply-to 123 --media-id <media_id> --kind image --filename example.png --key image-123-v1
```

一次发送最多一份媒体；`media_kind` 为 `image`、`voice`、`video` 或 `file`。上传不会自动发送。发送后用 `result --key <key>` 或 `qq_get_send_result` 确认 `sent`；`sent` 表示平台已受理。

主动消息不填 `reply_to`，使用 `scene + chat_id`。仅向部署环境变量精确允许名单中的授权目标发送；本地 Agent heartbeat 负责唤醒处理，不需要本地常驻服务。

## 运行说明

桥只能呈现官方事件实际收到并保存的会话，不是个人 QQ 全量历史或好友列表。`chat_id`/`user_id` 是平台 OpenID，不是 QQ 号。会话、资料和附件以服务端返回的实际字段为准。

附件 URL 可配合宿主的图像、音频或下载能力使用。发送动作统一保持幂等：同一 key 同内容返回原任务，同一 key 不同内容会冲突，从而避免重复投递。
