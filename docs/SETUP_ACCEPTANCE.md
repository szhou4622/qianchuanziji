# 配置验收记录

日期 2026-10-22，仓库 `szhou4622/qianchuanziji`，来源 v0.1.66 production-r31。

负责人 `szhou4622`，单人过渡模式批准人数 0，独立复核待补。保持公开可见性。源提交与服务器 ZIP 内嵌提交一致，22 个静态资源逐字节一致，来源文件 395 个，见 `source-r31.json`。

配置 [PR #1](https://github.com/szhou4622/qianchuanziji/pull/1)。[正常 CI](https://github.com/szhou4622/qianchuanziji/actions/runs/39741818622) 全部通过：四组共 1243 项测试，1242 通过、1 项沿用原测试的跳过条件；前端真实构建、全部 PHP 语法及仓库检查通过。

[受控失败 CI](https://github.com/szhou4622/qianchuanziji/actions/runs/39742814575)：临时移除任务分支 PR 模板，repository-checks 与 ci-gate 确实失败，依赖任务被跳过时 ci-gate 仍然失败，PR 回读为 BLOCKED。模板已恢复，恢复后的准确运行及合并后 main 运行见 [PR 验收说明与检查](https://github.com/szhou4622/qianchuanziji/pull/1/checks)，最终链接和合并提交记录于 PR 描述及负责人本机 verification.json。

main 经典保护已保存并重新读取：强制 PR、批准人数 0、新提交撤销旧批准、严格同步 main、必需 ci-gate 且来源绑定 GitHub Actions app 15368、管理员受约束、解决讨论、线性历史、禁止强推与删除、不锁分支。只允许 Squash、自动删已合并分支；Actions 默认 read，不能创建/批准 PR；密钥扫描与推送保护 enabled。

CI 环境移植记录：Windows runner 使用 China Standard Time；Node 22 显式安装。仅两处来源测试夹具调整：Node 通过 PATH 定位，临时路径先 resolve，保留原断言。业务运行代码未修改，原始摘要和改动原因都留在来源清单。完整测试分四组执行，不遗漏测试模块；最终 gate 核对各项实际成功。

原 main、commercial-v1 与来源 r31 完整历史已在本机 `D:\项目开发\qianchuanziji-audit-20261022` 保存为 bundle 并验证可读，保留用于恢复。服务器 ZIP 工作副本核对后清理。

已导入桌面端、前端、兼容服务端、测试及打包脚本，排除真实运行数据与秘密。CI 不使用正式授权数据，不执行真实投放，不自动部署。

范围外待办：独立成员复核、合法授权后桌面交互、完整新安装包构建、真实账户端到端投放和生产上线。本任务不更改现有服务器或客户端。
