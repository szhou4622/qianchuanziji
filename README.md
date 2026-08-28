# 千川商业版本地投放工具

本仓库用于开发新的 Windows 本地商业版千川投放工具。

## 开发基线

正式需求与架构以 `docs/baseline/` 下的 V1.2 Confirmed 文档为准。

当前开发阶段：**Phase 1：运行底座与新数据库**。

核心原则：

- 千川服务端事实优先；
- 不可信数据不得驱动自动动作；
- NULL 不等于 0；
- POST Unknown 不盲重发；
- 异常只冻结最小对象和最小能力；
- 用户在千川后台的人工操作优先；
- V1 不支持商品级策略；
- 激活服务器仅发生网络异常时，本地软件可正常使用 30 分钟，重复失败不得重新计时。

## Phase 1

阶段 1 只开发：

- Windows 单实例；
- Runtime Supervisor；
- SQLite WAL + FK + busy timeout；
- 单 Writer；
- Schema V1；
- Migration / Backup；
- Persistent Job；
- Lease / Heartbeat / Fencing；
- License Runtime State；
- DPAPI / Redaction；
- Diagnostics；
- 异常退出恢复底座。

阶段 1 禁止真实千川 POST，禁止旧数据库迁移，禁止商品级策略。
