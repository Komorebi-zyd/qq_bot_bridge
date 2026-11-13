# 云端升级：媒体 URL、身份资料与主动联系

## 本轮变更

- MCP 共 12 个工具，description 使用正向能力和参数说明。
- 发送支持 media_url + media_kind + 可选 filename；保留 media_id 上传方式。
- 自动积累官方事件的发言人昵称与角色，新增资料查询。
- 独立双向历史 API 保留旧入站 seq，新增 history_seq。
- 私聊回复默认 60 分钟/4 次、群聊 5 分钟/5 次；提供动态回复状态。
- 数值配置空值容错、分阶段发送诊断、旧数据库兼容迁移。

## 操作

1. 将更新后的代码提交并推送到你控制的仓库。真实 Secret 继续放在平台私密配置。
2. 保留 `qqbot-data` 原卷。建议先做平台存储快照或 SQLite backup，因为首次启动会新增表、索引并回填历史。
3. cloady 继续使用 `docker-compose.yml`、单 service `bridge`、8080、单副本，重新构建部署。
4. 保留原 AppID/AppSecret、桥密钥与实际域名 Host/Origin 配置。
5. 数值可选变量若旧值是空白，新版使用默认；旧配置显式写了 240 秒/1 次则按你实际配置运行，应检查并改成下方分场景值。

推荐配置：

```dotenv
BRIDGE_REPLY_WINDOW_C2C=3600
BRIDGE_REPLY_WINDOW_GROUP=300
BRIDGE_REPLY_WINDOW_GUILD=300
BRIDGE_REPLY_WINDOW_DM=300
BRIDGE_MAX_REPLIES_C2C=4
BRIDGE_MAX_REPLIES_GROUP=5
BRIDGE_MAX_REPLIES_GUILD=5
BRIDGE_MAX_REPLIES_DM=5
BRIDGE_MEDIA_URL_HOSTS=
BRIDGE_PROACTIVE_SESSIONS=c2c:你自己的实际OpenID
```

若旧版本曾配置 `BRIDGE_MAX_REPLIES_PER_MESSAGE=1`，检查并移除此统一覆盖项，改用分场景参数。需要继续保守限制时也可保留，能力接口会返回最终生效值。

主动发送的 OpenID 从 qq_list_sessions 获取，填精确 scene:chat_id。本文不嵌入任何实际用户 ID。QQ 客户端也需允许机器人主动发送；群聊由群主设置对应开关。

媒体 URL 可直接使用公开 HTTPS 直链。需要限定自有 CDN 时填 `BRIDGE_MEDIA_URL_HOSTS=cdn.example.com,files.example.com`。后端会检查公网地址，使用 QQ 平台 URL 上传，云桥不下载文件内容；链接的实际可达性和媒体兼容性由平台结果反映。

6. 访问 `/health`，然后调用 qq_status 和 qq_get_capabilities。
7. 在 Agent 的 MCP 设置里刷新工具列表或重新连接；应看到 12 个工具、新的正向描述和 media_url 参数。
8. 同步更新 Skill（若已安装），保留原心跳游标。

## 验收顺序

1. 真人发一条新私聊，检查回复时间与剩余次数。
2. 真人发一条群消息，检查实际收到的 sender 和资料缓存。旧消息原来没保存昵称时不保证可恢复。
3. 为自己的私聊授权后，显式发送一条主动文字，查询结果。
4. 分别测试小图片、短 MP4、语音直链；每次用独立业务 key，查看发送阶段和最终受理结果。
5. 查询双向历史，分别保存 history_seq 游标与入站 seq 游标。

本次本地修改不会自动更新你的 cloady 环境变量；推送部署与真实消息验收由后续步骤明确执行。
