# -skill-

私人技能合集。每个子目录是一个独立技能，可直接放进 `~/.workbuddy/skills/` 使用。

## 技能列表

| 技能 | 说明 |
|---|---|
| [minimax-sku-video](minimax-sku-video/) | 服装「图生视频」批量交付流水线：一个款号 → N 组 10 秒竖屏成片。十步流程，第 2 步是强制参考体检（代码级硬闸门），含提示词闸门、两阶段提交、收口质检 |
| [markitdown-skill](markitdown-skill/) | 用 MarkItDown 把 PDF / Word / PPT / Excel 等文档转成 Markdown |
| [totorosir-workbuddy-checkin](totorosir-workbuddy-checkin/) | WorkBuddy「Buddy 加油站」每日签到 + 派猫猫旅行，跨平台三通道 |
| [workdaddy-credit-expiry](workdaddy-credit-expiry/) | 给 WorkDaddy 面板加「积分到期明细」按钮，按到期日合并展示各账号积分到期；附命令行导出脚本。支持 Windows / macOS |

## 安装方式

**一键安装 / 更新（推荐）**：在 PowerShell 里粘这一行

```powershell
powershell -ExecutionPolicy Bypass -Command "iwr https://raw.githubusercontent.com/ggggoq/-skill-/main/install.ps1 -OutFile $env:TEMP\i.ps1; & $env:TEMP\i.ps1"
```

默认装全部技能；只装某一个加参数，例如：

```powershell
powershell -ExecutionPolicy Bypass -Command "iwr https://raw.githubusercontent.com/ggggoq/-skill-/main/install.ps1 -OutFile $env:TEMP\i.ps1; & $env:TEMP\i.ps1 -Skill minimax-sku-video"
```

更新时会把旧目录备份成 `<技能名>.bak-<时间>`，并保留你本地多出来的文件（如 `tools\ffmpeg.exe`、项目数据）。

**手动**：下载本仓库 ZIP，把对应子目录整个拷到 `~/.workbuddy/skills/<技能名>/`，或 `git clone` 后按需拷贝。

> 若第一条命令报 `赋值表达式无效` 之类的语法错误（个别机器上 `raw.` 返回的是缓存副本），改用 ZIP：下载 `main.zip` → 解压 → 进目录运行 `powershell -ExecutionPolicy Bypass -File .\install.ps1 -Skill minimax-sku-video`。ZIP 里的 `install.ps1` 永远是最新版。

## 说明

- 仓库里**不含任何 API Key、密钥、令牌**。需要 Key 的技能（如 minimax-sku-video）在安装后由主管单独发放。
- 每个技能的具体用法看各自目录下的 `SKILL.md`；`minimax-sku-video` 另有给新人看的环境配置手册（`assets/manual/环境配置手册.html`）。
