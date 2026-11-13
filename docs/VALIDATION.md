# 本轮离线验收

日期：2026-11-13。

## 通过

- 项目隔离虚拟环境：`python -m pytest -q`，**54 passed**；一条第三方 Starlette/AnyIO 弃用警告。
- `python -m pip check`：No broken requirements found。
- `python -m compileall -q bridge`：通过。
- `docker compose config --quiet`：使用占位凭据与测试域名通过语法校验。
- Skill frontmatter/结构校验通过，已打包 `dist/qq-official-bridge.skill`。
- 使用官方 MCP ClientSession 与 Streamable HTTP 客户端，在临时 loopback Uvicorn 服务完成初始化、工具发现及 8 工具调用契约测试。
- 鉴权、Host/Origin 检查、游标/ACK 历史、发送幂等与 unknown 不重发、重启持久化、媒体路径/大小/总量限制、附件 URL、SDK 签名/DTO 与群全量消息补丁使用 fake backend 验证。

## 尚未验收

- 没有读取真实机器人密钥，没有进行真实 QQ 收发或部署。
- Docker CLI 存在，但 Docker Desktop Linux 引擎未运行，因此未执行镜像构建与容器启动验收。
- cloady 实际入口、存储卷权限、官方平台权限、群全量事件、主动配额、富媒体编码/平台受理需要上线后验收。
- Session 持久化不等于消息零丢失；SDK 先推进网关 seq 后处理事件，崩溃窗口和平台不补发都可能造成未归档消息遗漏。

测试不会证明官方 Bot 零封禁，也不会证明任意音视频格式都能发送。`sent` 仅表示平台受理；`queued` 仅表示入队。
