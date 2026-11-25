---
name: workdaddy-credit-expiry
display_name: WorkDaddy 积分到期明细
display_name_en: WorkDaddy Credit Expiry Detail
description: 为 WorkDaddy 客户端（WorkBuddy / CodeBuddy 的账号管理面板）安装「积分到期明细」按钮，按到期日跨账号合并展示每个账号的积分到期情况，并用堆叠条形图显示账号占比；同时提供独立命令行脚本按账号逐笔导出积分到期时间。支持 Windows 与 macOS。当用户提到「WorkDaddy 积分到期 / 每个账号积分什么时候到期 / 积分快过期怎么看 / 给 WorkDaddy 加个按钮 / 积分到期分布看不懂」时使用。
description_zh: 为 WorkDaddy 客户端（WorkBuddy / CodeBuddy 的账号管理面板）安装「积分到期明细」按钮，按到期日跨账号合并展示每个账号的积分到期情况，并用堆叠条形图显示账号占比；同时提供独立命令行脚本按账号逐笔导出积分到期时间。支持 Windows 与 macOS。当用户提到「WorkDaddy 积分到期 / 每个账号积分什么时候到期 / 积分快过期怎么看 / 给 WorkDaddy 加个按钮 / 积分到期分布看不懂」时使用。
description_en: Installs a "credit expiry detail" button into the WorkDaddy client panel (the account manager for WorkBuddy / CodeBuddy), listing credit expiry merged by expiry date across accounts, with a 100%-stacked bar showing each account's share of that day. Also ships a standalone CLI that exports per-account, per-segment expiry times. Works on Windows and macOS. Handles WorkDaddy's built-in aggregated expiry popover, which merges all accounts and cannot show which account expires when.
category: 工具
agent_created: true
---

# WorkDaddy 积分到期明细

## 这个技能解决什么问题

WorkDaddy 面板自带的「积分到期分布」悬浮层，是把**所有账号**的积分按剩余天数聚合成桶（例如「7 天内 759」），看不到哪个账号、哪一笔、哪天到期。而每个账号的积分明细其实早就缓存在本机（`account-credit-cache.json`，按 uid 分开存），界面只是合并展示了。

本技能做两件事：

1. **给 WorkDaddy 面板加一个按钮**，点开按到期日合并列出：到期日 / 剩余天数 / 当天合计积分 / 账号占比堆叠条形图。
2. **提供命令行脚本**，按账号逐笔导出积分到期时间（可筛天数、可强制刷新实时数据、可输出 JSON）。

## 核心事实（不要重新摸索，直接引用）

- 注入锚点：WorkDaddy 的 `scripts/daemon.js` → `buildInjectScript()` 每次注入都**实时读** `scripts/inject.js`，并在锚点 `return { destroy: lifecycle.destroy, alive: lifecycle.alive };`（在 `build()` 函数体末尾）之前，拼入 `scripts/picker-internal.js`（仅当该文件存在）。**因此不需要改 `inject.js` 本体**，把自己的代码写进 `picker-internal.js` 即可，且注入位置共享 `accountsPane` / `state` / `el` / `listen` / `mountPersistentOverlay` / `applyI18n` / `registerDisposer` 闭包。
- 数据来源：`state.accounts[].creditSegments[]`，每项含 `remaining` / `expiresAt`（毫秒时间戳）/ `source`。不需要另调接口。
- 天数口径：自然日相减（目标日 00:00 − 今天 00:00），今天到期显示「今天」，已过期显示「已到期」。
- **账号数据只在打开面板时加载**：`state.accounts` 初始为空，面板未开时点按钮会显示 0 条，属正常现象。
- 更新会覆盖：Windows 用 `robocopy /MIR` 清安装目录，macOS 用 `rm -rf .app` + `cp -R`，注入文件都会丢，必须靠守卫恢复。
- 触发立即生效（免重启）：`POST http://127.0.0.1:<ui-port>/api/inject`，header `x-workdaddy-token`（读数据目录的 `.api-token`），body `{}`。`buildInjectScript()` 无缓存。

## 路径对照

| 用途 | Windows | macOS |
|---|---|---|
| 注入文件 | `%LOCALAPPDATA%\Programs\WorkDaddy\scripts\picker-internal.js` | `/Applications/WorkDaddy.app/Contents/Resources/scripts/picker-internal.js` |
| 数据目录 | `%APPDATA%\WorkDaddy` | `~/Library/Application Support/WorkDaddy` |
| 守卫 | 启动文件夹 `WD-CreditDetail-Guard.vbs` | `~/Library/LaunchAgents/com.workdaddy.creditdetail.guard.plist` |

macOS 另有三个 profile 的产物名：`WorkDaddy AI.app`、`CodeDaddy.app`、`CodeDaddy CN.app`，脚本会自动探测，也可用 `--app <路径>` 指定。

## 安装

前置：Node.js 18+（macOS 上 WorkDaddy 本身也依赖本机 Node，建议 22.13+）。

```bash
node scripts/wd-credit-detail.setup.js apps        # 先确认能探测到安装
node scripts/wd-credit-detail.setup.js install     # 安装
node scripts/wd-credit-detail.setup.js status      # 检查
node scripts/wd-credit-detail.setup.js uninstall   # 卸载
```

常用参数：`--app <路径>` 手动指定安装位置；`--dry-run` 只打印不写盘；`--no-inject` 不触发重新注入。

**macOS 权限**：`/Applications` 下的 `.app` 若不可写，安装会明确报错，用 `sudo node scripts/wd-credit-detail.setup.js install` 重试。该 app 自身未签名，改内部文件不会触发签名校验失败。

**Windows 权限**：若 `schtasks` 被安全策略拦截（本机实测 `EPERM`），不要试图绕过 —— 本脚本已改用启动文件夹方案，不需要计划任务权限。

## 安装后怎么用

打开 WorkDaddy 浮层面板 → 切到账号 tab（等数据加载）→ 点「总积分」右侧的列表按钮（在「汇总」星星旁边）。

## 命令行脚本

```bash
node scripts/list-credit-expiry.js                # 按账号逐笔列出到期时间（读本地缓存）
node scripts/list-credit-expiry.js --days 7       # 只看 7 天内到期的
node scripts/list-credit-expiry.js --refresh      # 先让 daemon 拉官方实时数据再列出
node scripts/list-credit-expiry.js --json         # 输出结构化 JSON
```

默认只读本地缓存，不发网络请求、不碰登录 token。

## 口径与设计取舍（用户已确认，不要擅自改）

- 按**到期日**合并，忽略时分秒，跨账号合并成一行。
- 条形每行**满 100%**，段宽 = 该账号在当天的积分占比；当天只有一家到期时整条满格。
- 代价：条长不再表达金额大小，金额对比靠数字列 + 橙色高亮（≥ 500）。这是明确确认过的取舍。
- 非今年的到期日显示成 `YY-MM-DD`，今年内显示 `MM-DD`（避免跨年数据看起来像排序错乱）。
- 数字列 `padding-right: 18px`，保证数字与条形之间有间距。

## 验证

```bash
node test/sim-mac.js
```

在 Windows 上伪造 macOS 目录树（`WD_FORCE_PLATFORM=darwin` + `WD_HOME` / `WD_APP_ROOTS` / `WD_NO_LAUNCHCTL` 环境变量），真实走完 mac 分支的安装/守卫/状态/卸载全流程，并校验合成后的注入脚本语法合法。当前 35 项断言全部通过。

## 排查

| 现象 | 原因与处理 |
|---|---|
| 按钮不出现 | 跑 `status` 看「已安装」是否为「是」；被更新冲掉后跑 `guard` 恢复 |
| 点了显示 0 条 | 面板未打开、`state.accounts` 为空；先开面板等账号加载 |
| mac 上安装报权限错 | 用 `sudo` 运行 |
| 更新后按钮消失 | 守卫会在登录时（mac 另有每 10 分钟）自愈；急用就手动 `guard` |
| 弹窗位置漂移 | 位置由按钮 `getBoundingClientRect()` 计算，面板收起时按钮坐标为 0，属正常 |

## 参考

`references/internals.md` 记录了 WorkDaddy 内部机制的关键源码位置与字段含义，改动前先读。
