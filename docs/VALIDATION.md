# 验收记录

日期：2026-11-13。

## 本轮更新离线验证

- 项目隔离虚拟环境：`python -m pytest -q -p no:cacheprovider`，**91 passed**；一条第三方 Starlette/AnyIO 弃用警告。
- `python -m pip check`：No broken requirements found。
- `git diff --check`：通过（Windows 行尾转换提示不影响结果）。
- `docker compose config --quiet`：占位凭据与测试域名校验通过。
- Skill 结构校验通过，重打包 `dist/qq-official-bridge.skill`，分发包排除了 Python 缓存。
- 官方 MCP ClientSession + Streamable HTTP 客户端 loopback 集成：12 工具发现、正向 description、REST/MCP 一致性、新能力/资料/双向历史路由。
- 四种媒体 URL 经过真实 SDK URL 上传函数及假 API，验证类型映射、srv_send_msg=false、富媒体DTO与持久化回复序号。
- URL语法、私网/混合DNS答案、允许域名与敏感URL错误脱敏测试。
- 旧数据库幂等迁移、旧key重复提交兼容、旧seq/ack保留、私聊新窗口计算、双向历史按原记录时间交错与重复启动稳定。
- 昵称快照、最新资料缓存、缺失昵称事件保持已知名字、mentions与附件字段。
- 主动会话允许项、回复额度、上传失败与发送未知结果区分、网关离线队列到期处理。

上述均使用假QQ后端和假DNS答案，更新期间未读取真实凭据、未触发真实QQ收发、未推送或部署。

## 旧线上版本已完成的真实验收

通过用户连接的MCP：
- QQ网关在线，last_error为空。
- 私聊文字接收成功；授权回复提交后返回sent。
- 图片附件链接成功打开（JPEG，1920×1440）。
- 语音附件收到原始URL、WAV URL和平台ASR；WAV成功下载，尚未独立转写核对。
- 主动发送曾被桥允许名单403拦截，用户已在QQ侧开启允许主动发送；桥端授权及新功能待云端更新后验证。

## 本轮待线上验证

- 新版本cloady镜像构建、首次实际卷迁移与网关恢复。
- URL图片/MP4/音频/文件的QQ真实受理与客户端展示。
- 真实群消息的昵称/角色字段丰富度。
- 精确授权后的主动发送、平台权限/配额行为。

本地Docker引擎未运行，未执行本轮镜像构建。已有线上旧版构建启动成功不等于新版本已部署。

网关Resume、平台补发和崩溃窗口仍影响未归档消息；保证已提交SQLite数据保留不等于所有消息零丢失。URL校验只针对提交与上传时的地址/DNS，文件实际由QQ平台请求，其后续解析/重定向行为由平台控制。
