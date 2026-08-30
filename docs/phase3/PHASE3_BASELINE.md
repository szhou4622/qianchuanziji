# 千川商业版 V1 — Phase 3 开发基线

状态：PASS

Gate：`docs/phase3/PHASE3_GATE.md`

前置条件：

```text
PHASE_2_GATE = PASS
```

## Phase 3 目标

只建立“可信 5 分钟热采集 → Latest / 5m 持久化 → 异常隔离”。

```text
ACTIVE_COLLECTING Plan
→ MATERIAL_5M / CONTROL_5M
→ Official GET
→ Pagination Integrity
→ Strict Normalize
→ Batch Trust Validation
→ Latest
→ Sparse 5m Snapshot
```

Phase 3 不执行任何自动策略，也不调用任何千川投放业务 POST。

## 本阶段新增允许 GET

```text
GET /open_api/v1.0/qianchuan/uni_promotion/ad/material/get/
GET /open_api/v1.0/qianchuan/uni_promotion/ad/control_task/list/
```

其他 Phase 2 GET 继续可用于计划状态确认、Token 与账户能力。

## 素材 5 分钟采集

只针对：

```text
monitor_enabled = true
lifecycle_state = ACTIVE_COLLECTING
collection_active = true
official_status = DELIVERY_OK
```

素材身份：

```text
advertiser_id + ad_id + material_id
```

热采集只接收官方 `material_status=DELIVERY_OK` 的视频素材。

每轮采集保存七个计划内素材指标：

```text
stat_cost_for_roi2
total_order_settle_amount_for_roi2_1h
total_prepay_and_pay_settle_roi2_1h
total_order_settle_count_for_roi2_1h
total_pay_order_count_for_roi2
total_pay_order_gmv_include_coupon_for_roi2
total_prepay_and_pay_order_roi2
```

这些指标严格属于 `advertiser_id + ad_id + material_id`。

## 调控任务 5 分钟采集

调控任务身份：

```text
advertiser_id + ad_id + control_task_id
```

官方原始任务 ID 字段使用：

```text
id
```

本地统一命名：

```text
control_task_id
```

热采集只读取官方 `PROCESSING` 任务，并显式请求七个任务级指标：

```text
stat_cost_for_roi2_assist
total_pay_order_count_for_roi2_assist
total_pay_order_gmv_include_coupon_for_roi2_assist
total_prepay_and_pay_order_roi2_assist
total_order_settle_amount_for_roi2_1h_assist
total_prepay_and_pay_settle_roi2_1h_assist
total_order_settle_count_for_roi2_1h_assist
```

## 数值规则

- 金额、ROI、预算、出价等保留官方原单位 Decimal 文本；
- 不经 API 契约证明，不执行“元→分”等换算；
- 数量字段使用 INTEGER；
- API 未返回字段必须保持 `NULL`；
- `NULL` 不得转换为 `0`；
- 非法数字使当前对象/批次不可用于自动业务，不静默修正。

## Latest 与 5m 快照

`*_latest`：

- 每次可信成功采集都更新 `collected_at`；
- 即使指标未变化也更新 freshness；
- 只保存最后一次可信服务器事实。

`*_5m`：稀疏写入，仅在以下情况创建快照：

```text
FIRST_SEEN
METRIC_OR_STATUS_CHANGED
RECOVERED_FROM_UNTRUSTED
```

Phase 4 以后可增加：

```text
STRATEGY_HIT
EXECUTION_EVIDENCE
```

禁止为了“每 5 分钟必须有一行”而保存大量完全相同历史行。

## SUSPICIOUS_EMPTY

如果：

```text
上一轮可信活动对象数 > 0
AND 当前分页技术成功且完整
AND 当前结果 = 0
AND 计划仍为 DELIVERY_OK
```

则当前结果不得直接证明“所有对象都消失”。

必须：

1. `collection_batch.status = SUSPICIOUS_EMPTY`；
2. 保留上一轮 Latest 数值；
3. 只冻结受影响的数据能力；
4. 对该类 Latest 标记为不可用于策略；
5. 安排短延迟复核；
6. 复核成功后再决定恢复、确认终态或继续异常。

素材异常不得冻结独立的调控任务采集；调控任务异常不得冻结独立的素材采集。

## 对象缺失

非空批次中，上一轮活动对象本轮未返回时：

- 不立即写 `DELETED` / `DISABLE` / “已结束”；
- 不伪造平台状态；
- Latest 保留最后可信数值；
- 对缺失对象标记 `MISSING_REQUIRES_CONFIRMATION`；
- 后续通过官方读取确认真实终态。

## 固定 5 分钟时钟

热采集按固定墙钟周期调度：

```text
HH:00
HH:05
HH:10
...
```

同一：

```text
advertiser_id + ad_id + pipeline_type
```

不得重叠执行。

若上一轮仍运行，当前周期记录 `SKIPPED_OVERLAP`，不在之后补跑旧实时周期。

电脑休眠 / 软件退出期间错过的实时 5 分钟周期同样不补跑。

## Phase 3 Gate 必须证明

- 素材 7 指标严格落到计划内素材；
- 调控任务 7 指标严格落到 task ID；
- 分页不完整绝不覆盖 Latest；
- 空结果不直接清空 Latest；
- `NULL` 不变 0；
- 相同 material_id 在不同 ad_id 下互不污染；
- 相同素材进入不同 control_task_id 时任务指标互不污染；
- Material / Control 两条流水可独立成功/失败；
- 同计划同类型无重叠；
- 重启不补旧 5 分钟周期；
- Phase 3 仍然无法调用业务 POST。
