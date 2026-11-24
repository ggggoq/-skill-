# workdaddy-credit-expiry

给 WorkDaddy 客户端加一个「积分到期明细」按钮：按**到期日合并**列出每个账号的积分到期情况，带账号占比堆叠条形图。支持 Windows / macOS。

## 为什么需要它

WorkDaddy 面板自带的「积分到期分布」把**所有账号**的积分按剩余天数聚合成桶（「7 天内 759」），看不出是哪个号、哪一笔、哪天到期。而每个账号的明细其实早就在本机缓存里，界面只是合并展示了。

## 效果

弹窗按到期日一行，显示当天合计积分 + 100% 堆叠条（段宽 = 该账号占当天比例）：

| 到期日 | 到期积分 | 账号占比 |
|---|---|---|
| 11-30 `7天` | **775.72** | 锅仔 80.6% + work 19.4% |
| 12-01 `8天` | 100.00 | work 100% |
| 12-11 `18天` | **500.00** | work 80% + 锅仔 20% |

橙色 = 单日 ≥ 500 的大额。跨年的日期显示成 `27-05-17` 形式，避免看起来像排序错乱。

样式预览：`preview/credit-expiry-demo-chart.html`

## 安装

需要 Node.js 18+。

```bash
node scripts/wd-credit-detail.setup.js apps        # 确认能探测到 WorkDaddy 安装
node scripts/wd-credit-detail.setup.js install     # 安装
node scripts/wd-credit-detail.setup.js status      # 检查状态
node scripts/wd-credit-detail.setup.js uninstall   # 卸载
```

参数：`--app <路径>` 指定安装位置 · `--dry-run` 只打印不写盘 · `--no-inject` 不触发重新注入。

安装后打开 WorkDaddy 浮层面板 → 账号 tab → 点「总积分」右侧的列表按钮。

## 命令行脚本

```bash
node scripts/list-credit-expiry.js              # 按账号逐笔列出到期时间
node scripts/list-credit-expiry.js --days 7     # 只看 7 天内到期的
node scripts/list-credit-expiry.js --refresh    # 先拉实时数据
node scripts/list-credit-expiry.js --json       # 结构化输出
```

只读本地缓存，不发网络请求、不碰登录 token。

## 实现方式

不修改 WorkDaddy 本体。利用它自带的一个注入钩子：`daemon.js` 的 `buildInjectScript()` 每次注入都会实时读 `scripts/inject.js`，并在锚点前拼入 `scripts/picker-internal.js`（存在才拼）。把自己的代码写进后者即可，且注入位置在 `build()` 函数体末尾，能直接共享 `state`、`accountsPane` 等闭包变量。

## 更新防护

WorkDaddy 自动更新会清空安装目录（Windows `robocopy /MIR`，macOS `rm -rf` + `cp -R`），注入文件必丢。安装脚本会注册守卫自动恢复：

- **Windows**：启动文件夹 VBS（`schtasks` 常被安全策略拦截，故不用计划任务）
- **macOS**：LaunchAgent plist（`RunAtLoad` + 每 10 分钟自检）

守卫检测到目标文件不含本补丁特征时，会先留底官方的 `picker-internal.js` 为 `.official`，再把两份内容合并写入，不覆盖对方。卸载时还原官方内容。

## 测试

```bash
node test/sim-mac.js
```

在非 mac 环境下伪造 macOS 目录树，真实走完安装/守卫/状态/卸载全流程，并校验合成后的注入脚本语法合法。当前 35 项断言全部通过。

## 已知限制

- 必须**先打开账号面板**再点按钮。`state.accounts` 只在打开面板时加载，否则显示 0 条。
- 条形固定满 100% 表示当天账号占比，因此条长不表达金额大小，金额对比看数字列。这是刻意取舍。
- macOS 上若 `/Applications` 下 app 不可写，需 `sudo` 运行安装。
