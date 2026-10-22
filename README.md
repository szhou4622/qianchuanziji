# QCSCKP 千川素材看盘与投放工具

唯一协作仓库：<https://github.com/szhou4622/qianchuanziji>。

本仓库导入 **v0.1.66 production-r31** 完整受版本管理源码：Python 桌面端、React 前端、PHP 兼容服务端、本地联调工具、测试与打包脚本。来源为 `szhou4622/qianchuan` 的 `qcsckp-production-v0.1.66-r31` 标签，原始提交 `91f2031cc9212382d36818730132afeaa93fd09e`。

服务器发布包的内嵌版本、原始提交及 22 个静态资源已核对一致。逐文件摘要、下载地址与发布包 SHA256 见 [来源清单](docs/source-r31.json)。源码 `release.json` 保留原开发身份；正式包身份由打包参数生成。

## 环境与启动

Windows 10/11 x64、Python 3.12、uv；界面需要 WebView2，浏览器采集需要本机 Google Chrome。前端构建使用 Node.js 22；兼容服务端需要 PHP 8.2 与 MySQL/MariaDB。

```powershell
git clone https://github.com/szhou4622/qianchuanziji.git
cd qianchuanziji/qcsckp-desktop
uv sync --frozen --no-install-project
$env:QCSCKP_HOME = Join-Path $env:LOCALAPPDATA 'QCSCKP-dev-qianchuanziji'
uv run --no-sync python -B startup_bootstrap.py
```

首次使用需合法授权。真实投放另行确认账户与操作范围。配置使用独立开发数据目录，不能复制正式数据库、Cookie 或授权凭据。应用读取进程环境，示例见 [.env.example](.env.example)。[桌面端原说明](qcsckp-desktop/README.md) 保留历史资料。

## 验证

```powershell
python -B tools/check_repository.py
cd qcsckp-desktop
uv run --no-sync python -B scripts/run_isolated_tests.py
cd production_v1a_frontend
npm ci --ignore-scripts
npm run build
```

CI 运行仓库规范、Windows 隔离 unittest、React/TypeScript 构建与全部 PHP 语法检查，最终必需检查为 `ci-gate`。前置检查失败、取消或跳过都不放行；CI 不执行真实投放或生产部署。

## 打包

在 Windows x64 / Python 3.12 依赖环境中手动执行：

```powershell
cd qcsckp-desktop
.\packaging\windows\build_windows.ps1 -Version 0.1.66 -Channel development -BuildRevision 1
```

输出在 `qcsckp-desktop/output/windows/`。正式发布另行审核新提交、渠道、构建号和 SHA256，不覆盖服务器旧包。macOS 工作流保留原手动/专用标签触发方式，需要独立签名 Secrets。

## 协作

阅读 [AGENTS.md](AGENTS.md)、[协作说明](docs/COLLABORATION.md) 和 [验收记录](docs/SETUP_ACCEPTANCE.md)。每项任务从最新 main 创建分支，通过 PR 和实际检查后 Squash 合并。当前单人过渡模式批准人数 0，独立复核待补。

本次导入源码和配置 GitHub 协作；生产服务器、安装版本与用户数据未修改。
