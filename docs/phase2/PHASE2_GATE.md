# 千川商业版 V1 — Phase 2 Gate

状态：PASS

通过基线提交：

```text
aa4572a3641a92966ae311d6dfbc9c39eed3f225
```

对应 CI：GitHub Actions run #70（run_id=33297166898）。

## Gate 结论

Phase 2 已完成以下能力并通过 Linux / Windows、Python 3.11 / 3.12 四矩阵测试：

- OAuth access_token / refresh_token 获取与持久化；
- Token 明确失效后最多强制刷新一次；
- 分页 Token 刷新不允许无限循环；
- OAuth/BP 主体与最终千川 advertiser_id 分离；
- EBP / Shop / PublicInfo 最终账户发现与落库；
- 单用户最多启用 10 个千川账户；
- 四类计划显式查询：乘方直播、乘方商品、全域直播、全域商品；
- 计划详情二次确认，分类冲突显式阻断；
- 单账户最多监控 10 个计划；
- 直播计划与商品计划生命周期分离；
- 商品计划不引入“开播/未开播”语义；
- 只有官方 `DELIVERY_OK` 进入 `ACTIVE_COLLECTING`；
- `DELETED` 对已监控计划进入 `TERMINAL`；
- `WATCHING` 计划执行独立 10 分钟“监控计划活跃状态检查”；
- 10 分钟状态检查不是全账户计划目录刷新；
- License Runtime Gate 会阻止无有效软件授权时继续调度；
- 金额 / 预算等未证明单位的金融数值按官方原值 Decimal 文本持久化，不擅自换算成分；
- 指标缺失保持 `NULL`，不猜测为 0；
- Phase 2 OpenApiClient 仍禁止所有千川投放业务 POST。

## CI 证据

最终 Gate CI 四个 Job 均通过：

```text
pytest (ubuntu-latest, 3.11)  PASS
pytest (ubuntu-latest, 3.12)  PASS
pytest (windows-latest, 3.11) PASS
pytest (windows-latest, 3.12) PASS
```

## 不属于 Phase 2 的内容

以下内容不因本 Gate 自动视为完成：

- 5 分钟素材热采集；
- 5 分钟调控任务热采集；
- `SUSPICIOUS_EMPTY`；
- Material / Control Latest 与 5m 快照写入；
- 策略判断；
- 候选冻结；
- 飞书确认；
- 千川业务 POST；
- 执行对账。

这些从 Phase 3 起继续实现。
