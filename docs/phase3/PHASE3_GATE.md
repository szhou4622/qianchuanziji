# 千川商业版 V1 — Phase 3 Gate

状态：PASS

通过基线提交：

```text
31af1dc043e0badb1cbfdf895b9a651068c6242d
```

对应 CI：GitHub Actions run #103（run_id=33301046761）。

## Gate 结论

Phase 3 已完成“可信 5 分钟热采集 → Latest / 稀疏 5m 持久化 → 异常隔离 → 官方证据确认”，并通过 Linux / Windows、Python 3.11 / 3.12 四矩阵测试。

通过后的核心边界仍然是：

> 宁愿停止受影响的最小能力，也不能让不可信数据继续驱动后续自动业务。

Phase 3 只建立可信读取与持久化基础，不执行策略，不进行飞书审批，不调用任何千川投放业务 POST。

## Phase 3 已证明的能力

- 仅 `ACTIVE_COLLECTING + DELIVERY_OK` 监控计划进入 5 分钟热采集；
- Material 与 Control 使用独立采集流水；
- 素材热采集显式请求并持久化 7 个计划内素材指标；
- 调控任务热采集显式请求并持久化 7 个 task 级指标；
- 调控任务官方原始 ID 使用 `id`，本地统一为 `control_task_id`；
- 金额、ROI、预算、出价按官方原值规范 Decimal 文本保存；
- API 缺失指标保持 `NULL`，禁止 `NULL → 0`；
- `*_latest` 每次可信采集刷新 freshness；
- `*_5m` 仅在 FIRST_SEEN / METRIC_OR_STATUS_CHANGED / RECOVERED_FROM_UNTRUSTED 等必要场景稀疏落库；
- 上一轮存在可信活动对象、当前完整读取突然为空时进入 `SUSPICIOUS_EMPTY`，不清空上一轮 Latest；
- 非空批次中对象突然缺失时进入 `MISSING_REQUIRES_CONFIRMATION`，不猜 `DELETED` / `DISABLE` / “已结束”；
- 异常对象通过短延迟、不带活动状态过滤的官方完整 GET 做证据确认；
- 只有明确读取到官方非活动状态才确认终态；仍找不到则继续保持不可用于策略；
- 确认任务属于 Durable Job，可在程序重启后恢复；实时 5 分钟周期不补历史；
- 固定墙钟 5 分钟调度，同一计划同一 pipeline 不重叠；
- Material / Control 异常相互隔离；一个流水失败不能冻结独立流水；
- 相同 `material_id` 在不同 `ad_id` 下保持完全独立的 Latest 与指标；
- 相同素材进入不同 `control_task_id` 时，task 指标不合并；
- 6 个热读取 Worker 已接入 Runtime；
- 单 advertiser 热读取并发硬限制不超过 2；
- 10 账户 × 10 计划（100 计划）公平调度压力测试通过；
- Worker 长任务具备 Lease heartbeat，Fencing 继续阻止过期 Worker 提交结果；
- Diagnostics 已暴露热采集队列、可信/异常对象、最近批次和并发峰值；
- Phase 3 OpenApiClient 仍然没有千川投放业务 POST 入口。

## 数据契约硬化

Phase 3 Gate 前额外补齐了“接口技术成功但业务语义不可信”的 fail-closed 行为。

### 活动素材过滤契约

热采集请求：

```text
material_status = DELIVERY_OK
```

如果官方成功响应反而返回其他 `material_status`：

```text
MATERIAL_ACTIVE_FILTER_MISMATCH
```

整批 `FAILED`，上一轮可信 Material Latest 不被覆盖，也不会把异常响应静默过滤后伪装为成功批次。

### 活动调控任务过滤契约

热采集请求：

```text
task_status = PROCESSING
scene = MATERIAL_ADD_BUDGET
```

如果官方成功响应违反状态或 scene：

```text
CONTROL_ACTIVE_FILTER_MISMATCH
CONTROL_SCENE_FILTER_MISMATCH
```

整批 `FAILED`，上一轮可信 Control Latest 不被覆盖。

### Token 刷新分页契约

如果分页中途明确发现 Token 失效：

```text
读取旧 Token page1
→ 旧 Token 后续页 Token 失效
→ 强制刷新 Token（最多一次）
→ 丢弃刷新前所有分页结果
→ 新 Token 从 page1 重新完整读取
```

禁止拼接刷新前后的分页数据，也禁止只从“失败的当前页”继续接着读。

## Gate 证据映射

| Gate 要求 | 主要测试证据 |
|---|---|
| 素材 7 指标严格落到计划内素材 | `tests/unit/test_phase3_hot_models.py`, `tests/unit/test_phase3_hot_collection.py` |
| 调控任务 7 指标严格落到 task ID | `tests/unit/test_phase3_hot_models.py`, `tests/unit/test_phase3_hot_collection.py` |
| 分页不完整绝不覆盖 Latest | `tests/unit/test_phase2_discovery.py`, `tests/unit/test_phase3_pagination_contracts.py` |
| Token 刷新后整批从第一页重读 | `tests/unit/test_phase3_pagination_contracts.py` |
| 响应违反活动过滤条件时 fail closed | `tests/unit/test_phase3_pagination_contracts.py`, `tests/unit/test_phase3_hot_collection_fail_closed.py` |
| 空结果不直接清空 Latest | `tests/unit/test_phase3_hot_collection.py` |
| 异常对象需要官方证据确认 | `tests/unit/test_phase3_hot_confirmation.py` |
| `NULL` 不变成 0 | `tests/unit/test_phase3_hot_models.py` |
| 相同 material_id 在不同 ad_id 不污染 | `tests/unit/test_phase3_gate_isolation.py` |
| 相同素材在不同 control_task_id 不污染 | `tests/unit/test_phase3_hot_collection.py` |
| Material / Control 可独立失败和成功 | `tests/unit/test_phase3_gate_isolation.py` |
| 同计划同 pipeline 不重叠 | `tests/unit/test_phase3_hot_scheduler.py` |
| 实时周期过期不补跑 | `tests/unit/test_phase3_hot_scheduler.py`, `tests/integration/test_startup_recovery.py` |
| 100 计划公平调度 + 单账户并发≤2 | `tests/stress/test_phase3_100_plan_fairness.py` |
| 单 Writer 并发安全 | `tests/unit/test_writer.py`, `tests/stress/test_sqlite_read_write_concurrency.py` |
| Phase 3 禁止业务 POST | `tests/unit/test_open_api_client.py` |

## CI 证据

最终 Gate CI 四个 Job 均通过：

```text
pytest (ubuntu-latest, 3.11)  PASS
pytest (ubuntu-latest, 3.12)  PASS
pytest (windows-latest, 3.11) PASS
pytest (windows-latest, 3.12) PASS
```

GitHub Actions：

```text
run #103
run_id = 33301046761
baseline commit = 31af1dc043e0badb1cbfdf895b9a651068c6242d
```

## Phase 3 明确没有完成的内容

本 Gate 不代表以下能力已经完成：

- 策略配置与不可变版本；
- HIT / NOT_HIT / NOT_EVALUABLE 策略求值；
- 多策略优先级仲裁；
- 候选冻结；
- 人工 / 自动执行模式；
- 飞书 Inbox / Outbox 与确认卡；
- 追投任务创建；
- 停投；
- 预算增加 / 时长调整；
- 千川业务 POST；
- POST outcome unknown 对账；
- 外部人工操作同步；
- 日 / 月 FINAL 结算。

这些必须在后续 Phase 独立实现并重新通过对应 Gate，不能因为 Phase 3 PASS 而默认成立。
