# Phase 1 Gate 验收记录

状态：**PASS**

```text
PHASE_1_GATE = PASS
```

代码基线分支：`commercial-v1`

Gate 代码/文档基线 HEAD：

```text
da8c53fb0aba7e89ffa62513733c59bb75e80187
```

GitHub Actions：

```text
Run #35
https://github.com/szhou4622/qianchuanziji/actions/runs/33294052871
结论：success
矩阵：Ubuntu / Windows × Python 3.11 / 3.12
```

## 已通过项目

- [x] 商业版独立目录建立
- [x] Windows 用户级单实例；正式 Windows 身份使用原生 SID
- [x] SQLite WAL / Foreign Key / busy timeout
- [x] 独立新库，不探测、不依赖旧生产数据库
- [x] Schema V1 完整创建
- [x] V1 无商品级策略核心表
- [x] Material 唯一键包含 advertiser_id + ad_id + material_id
- [x] Control Task 唯一键包含 advertiser_id + ad_id + control_task_id
- [x] 业务指标默认 NULL，不默认补 0
- [x] 单 Storage Writer
- [x] 50 线程 × 100 次 = 5000 次写入测试
- [x] 473 行事务第 220 行失败后整批回滚
- [x] Persistent Job
- [x] Job 只领取显式支持的 job_type
- [x] Lease / Heartbeat / Fencing
- [x] 旧 Worker Fencing Token 回写拒绝
- [x] 未知 Job 恢复默认 BLOCKED
- [x] 过期实时 5m Job 不补跑
- [x] Durable Reconciliation / Settlement / Outbox 可恢复
- [x] Runtime Supervisor
- [x] Watchdog
- [x] Startup Recovery
- [x] RUNNING Collection 重启后标记 ABORTED_BY_RESTART
- [x] 未完成 Execution 启动时只枚举待对账，不重发
- [x] 30 分钟 NETWORK_GRACE
- [x] 重复 License 网络失败不刷新 30 分钟起点
- [x] 明确 License 失效立即 INVALID
- [x] Windows DPAPI
- [x] 统一 Redaction
- [x] Diagnostics 中嵌入错误文本的 Token / Secret 也会脱敏
- [x] SQLite Backup API
- [x] Migration Framework
- [x] Migration 前备份
- [x] 更高版本 Schema 阻断旧 App 写入
- [x] 半完成 Migration 阻断 Runtime
- [x] 缺失关键表 / 索引阻断 Runtime
- [x] DB quick_check / write probe
- [x] DB 单文件 >8GB 不会单独机械停机
- [x] 50 Reader + 1 Writer 并发压力测试
- [x] Diagnostics 后端快照
- [x] GitHub Actions Ubuntu / Windows × Python 3.11 / 3.12 全部通过

## Phase 1 明确没有做的事情

以下内容未提前进入 Phase 1：

```text
真实千川 OAuth
真实 advertiser / plan 请求
Material Collector
Control Collector
Strategy Engine
Candidate
真实 Retarget POST
Stop POST
Budget Update POST
Duration Update POST
商品级策略
旧生产数据库迁移
```

因此 Phase 1 的 PASS 只代表：

> 商业版运行底座、新数据库、并发安全、持久化恢复和安全存储底座达到进入 Phase 2 的条件。

## 进入 Phase 2

允许进入：

```text
Phase 2：千川授权、账户与监控计划
```

Phase 2 首先开发只读/授权基础链：

```text
Open API Client
→ OAuth Token Provider
→ Advertiser Discovery
→ Account Identity
→ Plan Catalog / Detail
→ Four Plan Type Normalization
→ Monitor Plan Lifecycle
→ 10 分钟 WATCHING 状态检查
```

Phase 2 仍然禁止任何千川真实写 POST。OAuth Token POST 仅属于授权协议，不属于投放业务写操作。
