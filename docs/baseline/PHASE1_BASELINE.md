# 千川商业版 V1 — Phase 1 开发基线

状态：Confirmed

本文件把当前阶段必须遵守的正式基线固化到仓库。完整产品文档仍以已封板的六份 V1.2 Confirmed 文档为最终来源；如果本文件与正式文档冲突，必须停止冲突范围开发并回查正式文档。

## 六份正式文档

1. `01-产品需求文档-PRD-V1.2.md`
2. `02-技术架构设计文档-V1.2.md`
3. `03-数据库设计文档-V1.2.md`
4. `04-千川API与数据契约-V1.2.md`
5. `05-核心业务状态机与安全执行设计-V1.2.md`
6. `06-开发实施与测试验收文档-V1.2.md`

## 文档冲突优先级

```text
04 API事实
→ 01 PRD业务规则
→ 05 状态机与安全规则
→ 03 数据库约束
→ 02 技术实现建议
→ 06 开发顺序
```

## 已封板产品决策

- 商业版 V1 不支持商品级策略。
- 不迁移旧软件生产数据库，新商业版使用全新独立数据库。
- 激活服务器仅发生网络异常时，本地软件进入 30 分钟 `NETWORK_GRACE`，期间软件正常使用。
- `NETWORK_GRACE` 从本轮首次网络失败开始计时，后续网络失败不得重新续 30 分钟。
- 30 分钟内服务器恢复并确认授权有效：恢复 `ACTIVE`。
- 30 分钟内服务器明确返回失效、过期、禁用、设备不匹配：立即 `INVALID`，不等待宽限结束。
- 30 分钟到期仍不可达：停止新业务；已经发送但未确认的 Execution 后续只允许安全只读对账。

## 全局安全不变量

- 千川服务端事实优先。
- 不可信数据不得驱动自动动作。
- `NULL` 不得猜测为 0。
- POST Outcome Unknown 不得盲重发。
- 异常只冻结最小对象和最小能力。
- 软件不得对抗用户在千川后台的人工操作。
- 官方状态与本地状态必须分离。
- 所有业务写统一由 SQLite 单 Writer 执行。
- 网络请求期间不得持有 SQLite 写事务。
- Worker Lease 失效后旧 Fencing Token 不得回写。
- 过期 5 分钟实时任务不补跑。
- 旧数据库不是 Runtime 依赖。

## Phase 1 范围

只开发：

- Windows 用户级单实例；
- Runtime Supervisor / Watchdog；
- SQLite WAL + FK + busy timeout；
- Schema V1；
- 单 Writer；
- Persistent Job；
- Lease / Heartbeat / Fencing；
- License Runtime State；
- DPAPI / Redaction；
- SQLite Backup；
- Migration Framework；
- Database Health；
- Diagnostics；
- Startup Recovery。

禁止提前开发：

- 真实千川 OAuth；
- 真实账户 / 计划采集；
- Material / Control Collector；
- Strategy / Candidate；
- Retarget / Stop / Budget / Duration POST；
- 商品级策略；
- 旧数据库自动迁移。

## Phase 1 Gate 核心条件

- Windows SID 级单实例真实测试通过；
- 50 并发提交、5000 次写由单 Writer 串行完成；
- 473 行事务第 220 行失败后整批 0 行提交；
- Schema V1 全表存在且无商品级核心表；
- 指标字段默认 `NULL`；
- Lease/Fencing 旧 Worker 回写被拒绝；
- `NETWORK_GRACE` 固定 30 分钟且不续命；
- DPAPI Windows round-trip 通过；
- 诊断输出不得泄漏 Token / Secret / 激活码 / Device Credential；
- Migration 前使用 SQLite Backup API；
- 半完成 Migration、缺失关键表/索引、更高 Schema 均阻断 Runtime；
- DB 单文件超过 8GB 不能单独成为硬停条件；
- 50 Reader + 1 Writer 并发压力通过；
- Startup Recovery 不补旧 5m 实时任务、不重发未确认 Execution；
- Ubuntu / Windows × Python 3.11 / 3.12 CI 全部通过。
