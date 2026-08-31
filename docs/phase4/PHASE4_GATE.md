# 千川商业版 V1 — Phase 4 Gate

状态：PASS

通过基线提交：

```text
cd8b793d037a87eb157664ad1ca30e211dce76ee
```

对应 CI：GitHub Actions run #121（run_id=33391927314）。

## Gate 结论

Phase 4 已完成“可信热采集 → 本地策略求值”的确定性策略基础链路，并通过 Linux / Windows、Python 3.11 / 3.12 四矩阵测试。

本 Gate 确认以下能力成立：

- 策略层只消费 Phase 3 已持久化的可信 `SUCCESS` 热采集批次；
- `SUSPICIOUS_EMPTY`、失败批次、非可信 Latest 不得产生策略 HIT；
- 策略求值不访问千川网络，不执行任何投放业务 POST；
- V1 策略条件逻辑仅支持 `AND`；
- 数值使用 Decimal 语义比较；
- 指标缺失 / `NULL` 进入 `NOT_EVALUABLE`，绝不猜成 0；
- 三态结果固定为 `HIT / NOT_HIT / NOT_EVALUABLE`；
- 策略只允许使用对应对象的可信 V1 指标白名单；
- 账户级 Topic 特殊指标不能进入严格计划级自动策略；
- V1 当前禁止商品级策略类型；
- 策略版本不可原地覆盖，修改会生成新的不可变 `strategy_version`；
- `strategy_hit` 保存策略版本、来源 batch、来源采集时间、条件快照和指标快照；
- 同一对象、同一动作、同一来源批次命中多个策略时执行确定性优先级仲裁；
- 高优先级策略获得执行资格；低优先级 HIT 保留审计记录并标记 `SUPPRESSED_BY_HIGHER_PRIORITY`；
- 同优先级使用稳定 strategy_id 顺序确定赢家，避免线程时序影响结果；
- 不同动作类型互不压制，例如停止与预算增加可分别命中；
- HIT ID 使用确定性键生成，同一策略版本 + 同一对象 + 同一来源 batch 重复求值不会重复落库；
- 成功热采集只有在该计划确有对应对象类型的启用策略时才创建策略 Job；
- 完全没有启用策略时，不产生无意义空策略 Job；
- Material / Control 策略 Job 使用独立类型和独立本地 Strategy Worker，不占用官方 GET 热读池；
- 软件 License 不允许业务运行时，Strategy Handler 不继续求值或产生新的 HIT；
- Strategy Job 属于 Durable Job，重启后可重新排队；因 HIT 幂等，所以可安全重放；
- Diagnostics 已暴露启用策略数、策略队列、HIT 数、被压制 HIT 数及最近 HIT；
- Phase 2/3 既有账户、计划、热采集、诊断契约未被破坏。

## CI 证据

最终 Gate CI 四个 Job 均通过：

```text
pytest (ubuntu-latest, 3.11)  PASS
pytest (ubuntu-latest, 3.12)  PASS
pytest (windows-latest, 3.11) PASS
pytest (windows-latest, 3.12) PASS
```

Ubuntu Python 3.11 基线结果：

```text
113 passed, 3 skipped
```

## Phase 4 明确没有做的事情

以下内容不因本 Gate 自动视为完成：

- 候选冻结 / Candidate Batch；
- 追投素材分组与最多 20 素材拆组；
- 追投活跃任务 Guard；
- 飞书确认卡片；
- 手动确认 30 分钟过期；
- 自动模式直接转 Execution；
- 千川业务 POST；
- `execution_attempt` 写前日志；
- POST Unknown 处理；
- 创建追投任务后的 GET Reconciliation；
- 调控任务停止 / 预算增加 / 时长延长实际执行；
- 外部人工修改后的执行前 Preflight；
- Daily / Monthly 精确窗口结算；
- Retention / Maintenance 完整落地；
- Windows UI / Tray / Update 完整商业发布链路。

这些进入后续阶段继续实现。

## Phase 5 入口条件

进入下一阶段前保持以下硬约束不变：

1. 候选只能来自未被压制的有效 HIT；
2. 候选必须冻结策略版本、指标快照、执行参数和对象集合；
3. 候选生成与千川 POST 必须完全分层；
4. 手动确认看到的是冻结候选，确认时不得静默重新选素材；
5. 千川服务器事实优先，执行前必须重新读取必要的当前状态；
6. 不可信数据、过期候选、外部人工变化都必须阻止错误执行，而不是自动“纠正”用户；
7. Phase 5 即使开始设计 Execution，也不得提前解除现有 OpenApiClient 对千川投放业务 POST 的安全阻断，直到对应写接口契约、写前日志、幂等/Unknown/Reconciliation 测试全部就绪。
