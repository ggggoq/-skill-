# WorkDaddy 内部机制参考

## 注入链路

```
daemon.js  buildInjectScript()
  ├─ 读 scripts/inject.js（每次实时读，无缓存）
  ├─ 在锚点前拼入 automation-picker.js
  ├─ 在锚点前拼入 scripts/picker-internal.js（存在才拼）
  │     锚点 = "return { destroy: lifecycle.destroy, alive: lifecycle.alive };"
  │     该锚点位于 build() 函数体末尾 → 拼入的代码共享 build() 闭包
  └─ 通过 CDP 注入到 WorkBuddy/CodeBuddy 页面
```

关键源码位置（版本 1.2.22）：

| 位置 | 作用 |
|---|---|
| `scripts/daemon.js:4640` 附近 | `buildInjectScript()` 主体，实时读 inject.js |
| `scripts/daemon.js:4757` | 注释：`picker-internal.js` 存在才注入（git 不跟踪） |
| `scripts/daemon.js:4761` | `pickerPath = path.join(__dirname, 'picker-internal.js')` |
| `scripts/inject.js:18277` | 锚点所在行 |
| `scripts/inject.js:6844` | `accountsPane` 定义（`var`，函数作用域级，锚点处可见） |
| `scripts/credit-segments.js` | `extractCreditSegments`，分段展开逻辑 |
| `scripts/credit-resource-queries.js` | 官方接口 body：`POST /v2/billing/meter/get-user-resource` |
| `scripts/inject.js:2508` | `summarizeCreditDays`，原生「积分到期分布」的聚合算法 |
| `scripts/theme-vars.js` | 主题变量定义 |

## 数据字段

`state.accounts[]` 每项（本技能用到的）：

| 字段 | 含义 |
|---|---|
| `uid` | 账号唯一 id |
| `nickname` | 显示名 |
| `creditSegments[]` | 积分分段数组 |
| `creditSegments[].remaining` | 该段剩余积分（Number） |
| `creditSegments[].expiresAt` | 该段到期时间，毫秒时间戳；可能为 null |
| `creditSegments[].source` | 来源标签 |
| `creditUnlimited` / `creditExpired` | 无限量 / 已过期，需跳过 |

本地缓存文件：`<数据目录>/account-credit-cache.json`，结构是 `{ [uid]: { creditSegments: [...] } }`。

## 到期时间取值规则

`extractCreditSegments` 在多个候选字段中取**最早**值：月度包取 `CycleEndTime` 而非 `DeductionEndTime`。这意味着同一笔积分在不同字段下日期可能不同，取值规则不能想当然。

## 天数口径

界面原生算法：

```js
Math.ceil((expiresAt - now) / 86400000)
```

本技能按「到期日 23:59:59」计算，即 `ceil((当天23:59:59 - now) / 86400000)`，所以 11-30 到期的积分在 11-24 显示 7 天。

## 本机 API

| 接口 | 方法 | 鉴权 | 说明 |
|---|---|---|---|
| `/api/inject` | POST | `x-workdaddy-token` | 触发重新注入，返回 `{"ok":true,"mounted":true}` |
| `/api/accounts` | GET | 同上 | 返回账号列表（含 nickname） |
| `/api/credits` | GET | 同上 | 查询并回写积分缓存 |

端口读 `<数据目录>/ui-port.json`，token 读 `<数据目录>/.api-token`。

## 更新覆盖机制

| 平台 | 更新方式 | 后果 |
|---|---|---|
| Windows | `robocopy /MIR`（`scripts/apply-update.ps1`） | 镜像覆盖安装目录，注入文件被删 |
| macOS | `apply-update.sh`：`mv` 旧 `.app` 到 `.old` → `cp -R` 新 `.app` → `xattr -cr` | 整个 bundle 被替换，注入文件被删 |

macOS 的 `build-mac-dmg.sh` 里明确有这段：

```bash
if [ -f "scripts/picker-internal.js" ]; then
  cp "scripts/picker-internal.js" "$APP/Contents/Resources/scripts/picker-internal.js"
else
  rm -f "$APP/Contents/Resources/scripts/picker-internal.js"
fi
```

说明官方构建时若自己带了 `picker-internal.js` 会一起打包。因此守卫脚本设计成**追加而非覆盖**：检测到目标文件不含本补丁特征串时，先留底为 `.official`，再把官方内容 + 我方代码一起写入；卸载时还原官方内容。

## 主题变量

WorkDaddy 只暴露少量 CSS 变量，弹窗样式必须带 fallback：

| 变量 | 用途 |
|---|---|
| `--wb-color-text-primary` | 主文字 |
| `--wb-color-text-secondary` | 次要文字 |
| `--wb-color-text-tertiary` | 表头等弱化文字 |
| `--wb-bg-tertiary` | 浅底（条形轨道、天数徽标） |
| `--wb-border-subtle` | 行分隔线 |
| `--wb-border-strong` | 未知到期日的条 |
| `--wb-bg-popover` | 弹窗背景 |

账号配色不走主题变量，用固定色板按账号顺序分配：`#22c55e` / `#3b82f6` / `#f59e0b` / `#a855f7` / `#ec4899` / `#06b6d4`。
