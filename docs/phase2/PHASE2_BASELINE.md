# 千川商业版 V1 — Phase 2 开发基线

状态：IN PROGRESS

前置条件：

```text
PHASE_1_GATE = PASS
```

## Phase 2 目标

只建立千川官方授权、账户身份、计划目录/详情和监控计划生命周期。

```text
Open API Client
→ OAuth Token Provider
→ Advertiser Discovery
→ Final advertiser_id
→ Plan Catalog / Detail
→ Four Plan Type Normalization
→ Monitor Plan
→ LIVE / PRODUCT lifecycle
→ 10分钟 WATCHING 状态检查
```

## Phase 2 允许调用的官方接口

OAuth：

```text
POST /open_api/oauth2/access_token/
POST /open_api/oauth2/refresh_token/
```

账户：

```text
GET /open_api/oauth2/advertiser/get/
GET /open_api/v1.0/qianchuan/shop/advertiser/list/
GET /open_api/2/ebp/advertiser/list/
GET /open_api/2/advertiser/public_info/
```

计划：

```text
GET /open_api/v1.0/qianchuan/uni_promotion/list/
GET /open_api/v1.0/qianchuan/uni_promotion/ad/detail/
```

本阶段禁止调用任何投放业务写接口。

## 官方基础事实

默认 Open API Host：

```text
https://api.oceanengine.com
```

最终千川账户身份优先使用：

```text
/open_api/2/ebp/advertiser/list/
account_type=QIANCHUAN
→ account_id
→ local advertiser_id
```

OAuth/BP 主体不能直接当作最终千川 advertiser_id。

四类计划：

```text
OVERALL_PROJECT + LIVE_PROM_GOODS  = OVERALL + LIVE
OVERALL_PROJECT + VIDEO_PROM_GOODS = OVERALL + PRODUCT
UNI_PROJECT + LIVE_PROM_GOODS      = UNI + LIVE
UNI_PROJECT + VIDEO_PROM_GOODS     = UNI + PRODUCT
```

只有官方：

```text
DELIVERY_OK
```

表示投放中。

`DELETED` 对已监控计划可作为终态。

## 计划生命周期

直播计划：

```text
未达到直播/投放资格 → WATCHING → 10分钟状态检查
恢复有效 + DELIVERY_OK → 立即首采准备 → ACTIVE_COLLECTING
下播/不投放 → WATCHING
DELETED → TERMINAL
```

商品计划：

```text
DELIVERY_OK → ACTIVE_COLLECTING
非 DELIVERY_OK 且未删除 → WATCHING
DELETED → TERMINAL
```

商品计划没有“开播”概念。

## 10分钟任务边界

10 分钟任务只检查：

> 用户已经选择监控、当前处于 WATCHING 的计划。

它不是全账户 Plan Catalog Refresh。

完整计划目录刷新使用独立任务：

```text
PLAN_CATALOG_REFRESH
```

## Phase 2 安全边界

- Token refresh 后分页必须从第一页重来；
- 不拼接刷新前后的分页；
- 一个账户授权失败只冻结该账户；
- 未知计划 scene/status 保留 raw，不猜；
- plan_system / promotion_scene 冲突时进入分类冲突，不自动推断；
- Phase 2 不做 Material / Control Strategy；
- Phase 2 不做任何千川投放业务 POST；
- V1 不支持商品级策略。
