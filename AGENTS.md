# 工作范围

- 唯一目标仓库 `szhou4622/qianchuanziji`；主克隆 `D:\项目开发\qianchuanziji`，允许此仓库的独立 clone/worktree。
- 开工核对远程、分支、未提交改动和相关 PR，保留用户工作。不修改其他仓库、生产服务器、数据库或下载目录。
- 基线为 v0.1.66 production-r31，原始提交和文件摘要见 `docs/source-r31.json`，原始 Git 历史在本机审计目录保留。

# 开发与验证

- 从最新 origin/main 建任务分支，续做未合并任务用原分支；不强推共享历史。
- 逐文件检查差异，只提交当前任务；通过 PR Squash 合并，不能绕过保护。
- 运行 `python -B tools/check_repository.py`；桌面改动运行 `qcsckp-desktop/scripts/run_isolated_tests.py`；前端运行 `npm ci --ignore-scripts` 和 `npm run build`；PHP 修改执行 `php -l`。
- 测试只用隔离数据和模拟凭据，不执行真实千川写入。隔离脚本会阻断外网。
- 不修改业务逻辑凑 CI 绿灯，原有失败先定位并明确报告。
- 合法修改导入文件时，在来源清单的 `documented_changes` 中记录路径与原因；不删除来源校验或批量重算原始摘要。

# 配置和交付

- 不提交真实 `.env`、`config.php`、数据库、Cookie、DPAPI 凭据、Token、私钥或日志。`local-test/config.local.php` 是无秘密的外部文件适配器，可保留。
- 真实投放、生产部署、可见性变更与成员邀请需另有明确授权。
- GitHub 实际规则强制保护 main，本文件不能代替保护。当前单人过渡模式批准人数 0，独立复核待补。
- 清理本任务临时文件，保留交付和必要审计记录，按全局 Windows 精确路径删除规则执行。
- 报告范围、分支/提交/PR、实际检查、未验证事项、合并及上线状态、恢复办法。
