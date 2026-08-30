# 千川商业版本地投放工具

本仓库用于开发新的 Windows 本地商业版千川投放工具。

## 开发基线

正式需求与架构以 V1.2 Confirmed 文档为准。

仓库内已固化：

- `docs/baseline/PHASE1_BASELINE.md`
- `docs/phase1/PHASE1_GATE.md`

## 当前状态

```text
PHASE_1_GATE = PASS
当前阶段 = Phase 2：千川授权、账户与监控计划
```

Phase 1 已通过 Ubuntu / Windows × Python 3.11 / 3.12 CI Gate。

核心原则：

- 千川服务端事实优先；
- 不可信数据不得驱动自动动作；
- NULL 不等于 0；
- POST Unknown 不盲重发；
- 异常只冻结最小对象和最小能力；
- 用户在千川后台的人工操作优先；
- V1 不支持商品级策略；
- 激活服务器仅发生网络异常时，本地软件可正常使用 30 分钟，重复失败不得重新计时。

## Phase 2

当前按以下顺序开发：

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

Phase 2 禁止任何千川投放业务写 POST。OAuth Token POST 只用于官方授权协议。
