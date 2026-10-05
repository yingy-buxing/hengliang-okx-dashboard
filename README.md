# 衡量 · OKX 账户分析看板

通过自己的只读 API，在本机查看资产与持仓、交易成本、历史权益和风险指标。支持历史文件补充、本机持久化、账户历史备份与离线查看。

## 启动

需要 Python 3.10 或更新版本。网页服务不需要安装第三方 Python 包。

```sh
python account-dashboard/server.py
```

macOS/Linux 可使用 `python3`。启动后打开 http://127.0.0.1:4180/ 。macOS 也可双击 `account-dashboard/启动看板.command`。

先点击“先用示例数据体验”，或填写自己的 OKX API Key、Secret Key 和 Passphrase。仅接受读取权限。

## 功能

- 资产、持仓与快照自动刷新，金额变化动画。
- 自定义分析日期与快捷区间，保存日期设置，支持结束日期始终到今天。
- TradingView Lightweight Charts 交互权益与回撤曲线。
- 成交筛选、交易轮次、多空表现与成本拆分。
- API 近期历史与本机记录去重合并；历史 ZIP/CSV 导入前预览。
- 账户历史备份、恢复校验、离线读取历史缓存。
- macOS 钥匙串与 Windows 凭据管理器保存账户。

## 数据与安全边界

仅监听本机，不是多人在线服务。凭据不写入浏览器存储或普通配置文件；勾选记住账户时使用系统凭据存储。账户数据库与导出仍是私有数据，仓库不包含任何真实账户记录、凭据或截图。

风险指标来自历史流水与价格重建估算，不能视为 OKX 官方净值或日内最大回撤。缺失或核对失败的数据会标记，当前主要复核 USDT 线性永续。Windows 保存已完成模拟测试，原生 Windows 测试仍需在 Windows 运行确认。

详细功能、计算口径及限制见 [看板说明](account-dashboard/README.md)。

## 测试

```sh
python -m unittest discover -s account-dashboard -p "test_*.py"
```

可选的前端纯函数检查需要 Node.js：

```sh
node account-dashboard/test_numbers.cjs
node account-dashboard/test_period.cjs
node account-dashboard/test_live.cjs
```

保存测试使用隔离的假凭据条目，不访问 OKX。`verify.py` 属于需要真实凭据的本机只读验收工具，不是默认测试。

## 第三方组件

Lightweight Charts 5.2.1 使用 Apache-2.0，LICENSE、NOTICE 和版本信息随组件保留在 `account-dashboard/public/vendor/`。
