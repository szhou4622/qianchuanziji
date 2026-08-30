# 千川商业版 V1 — Phase 4 开发基线

状态：IN PROGRESS

前置条件：

```text
PHASE_3_GATE = PASS
```

## Phase 4 目标

只建立“可信 Latest → 确定性策略求值 → 命中证据 → 同动作优先级仲裁”。

```text
Trusted Latest
→ Strategy Config
→ Immutable Strategy Version
→ Tri-state Evaluation
→ HIT Evidence
→ Same-object / Same-action Arbitration
```

Phase 4 不创建执行候选、不发飞书确认、不调用任何千川投放业务 POST。

## V1 策略对象

V1 不支持商品级策略。

只允许：

```text
MATERIAL_RETARGET
  object = MATERIAL
  action = CREATE_RETARGET

CONTROL_STOP
  object = CONTROL_TASK
  action = PAUSE_CONTROL

CONTROL_BUDGET_INCREASE
  object = CONTROL_TASK
  action = UPDATE_BUDGET

CONTROL_DURATION_EXTEND
  object = CONTROL_TASK
  action = UPDATE_DURATION
```

策略作用域 Phase 4 先固定为单监控计划：

```text
target_scope = PLAN:<target_uid>
```

禁止把同一策略跨计划隐式复用，后续如需账户级/模板级策略必须独立设计。

## 条件逻辑

同一策略版本内部所有条件只支持：

```text
AND
```

不支持 OR，不做隐式括号推理。

条件结构：

```json
{
  "logic": "AND",
  "conditions": [
    {"field": "overall_cost_decimal", "op": "GTE", "value": "100"},
    {"field": "net_settle_roi_decimal", "op": "LT", "value": "2"}
  ]
}
```

支持比较操作符：

```text
GT
GTE
LT
LTE
EQ
NE
```

所有数值比较使用 Decimal，禁止 float 参与策略判定。

## 三态求值

单条件结果：

```text
HIT
NOT_HIT
NOT_EVALUABLE
```

AND 聚合：

```text
任一条件 NOT_HIT       → NOT_HIT
否则任一 NOT_EVALUABLE → NOT_EVALUABLE
全部 HIT                → HIT
```

任何策略依赖字段：

- API 未返回；
- 本地为 NULL；
- 数据对象不是 TRUSTED；
- 对象 `strategy_eligible != 1`；
- 字段超出当前对象官方已确认指标集合；

都不得被猜成 0，也不得产生 HIT。

## Material 可用于自动策略的字段

只允许 Phase 3 已确认的 7 个计划内字段：

```text
overall_cost_decimal
net_settle_amount_decimal
net_settle_roi_decimal
net_settle_order_count
overall_order_count
overall_gmv_decimal
overall_pay_roi_decimal
```

## Control Task 可用于自动策略的字段

只允许 Phase 3 已确认的 7 个 task 级字段：

```text
assist_cost_decimal
assist_order_count
assist_gmv_decimal
assist_pay_roi_decimal
assist_net_amount_decimal
assist_net_roi_decimal
assist_net_order_count
```

预算、时长、任务状态可作为执行前置证据，但 Phase 4 不把它们混入“7 个业务指标”策略字段集合。

## 策略版本

`strategy_version` 必须不可变。

用户修改任一：

- 条件；
- action 参数；
- grouping_mode；
- priority；

都必须创建新版本：

```text
v1 → v2 → v3
```

旧版本永不 UPDATE 覆盖。

`strategy_config.current_version_id` 只指向当前版本。

启用/停用是配置状态，不改写历史版本内容。

## 命中证据

`strategy_hit` 只持久化真实 HIT（包括被更高优先级压制的 HIT）。

普通 `NOT_HIT` 不落大量历史行；`NOT_EVALUABLE` 通过当前求值结果/诊断暴露，不伪装成未命中。

每条 HIT 必须冻结：

- strategy_id；
- strategy_version_id；
- target_uid；
- object identity；
- advertiser_id / ad_id；
- source_batch_id；
- source_collected_at；
- condition_snapshot_json；
- metric_snapshot_json；
- suppression_reason；
- winner_strategy_id。

Phase 5 候选只能使用已经落库的 HIT，不允许重新用“当前 Latest”偷换命中证据。

## 多策略优先级仲裁

只在以下完全相同的冲突域内仲裁：

```text
same object
+ same action_type
+ same source batch / evaluation cycle
```

按：

```text
priority DESC
strategy_id ASC
```

确定唯一 winner。

低优先级策略仍保留 HIT 事实，但写：

```text
suppression_reason = SUPPRESSED_BY_HIGHER_PRIORITY
winner_strategy_id = <winner>
```

不同 action_type 不互相压制。

## Phase 4 Gate 必须证明

- 只接受 V1 明确允许的策略类型与对象字段；
- 策略条件全部 AND；
- Decimal 比较无 float；
- NULL / 缺字段必为 NOT_EVALUABLE，绝不转 0；
- 不可信 Latest 不能产生 HIT；
- strategy_version 不可变，修改只能生成新版本；
- 同对象同动作同批次只产生一个 winner；
- 相同优先级时结果仍确定性一致；
- 不同 action_type 不互相压制；
- HIT 证据引用准确 source_batch_id / source_collected_at；
- Phase 4 不创建 candidate_batch；
- Phase 4 仍然无法调用千川投放业务 POST。
