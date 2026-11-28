---
name: minimax-sku-video
description: 服装「图生视频」批量交付流水线（MiniMax H3 / 9:16 / 10 秒）。当用户说「开始生成+款号」「这批图生视频」「接新款」「第 N 组重出」「这版 BGM 不对」「换参考片再跑」「不同款动作怎么都一样」「查一下这几条参考是不是同一条」，或需要一键建项目骨架、建款目录、跑参考体检、跑骨架分镜、写提示词、提交任务、收口质检时使用。覆盖从素材盘拷素材、参考重复体检、窗口选段、提示词闸门、两阶段提交、下载收口到交付归档的全流程。
agent_created: true
---

# 图生视频批量交付流水线

把「一个款号 → N 组 10 秒竖屏种草视频」固化成一串脚本 + 闸门，换人接手不用重新试错。
模型固定 `minimax-h3-pro-768p`，10 s / 9:16 / 768×1344 / 24 fps，单次调用 US$0.373。

## 何时用

- 「开始生成 + <款号>」「接这个款」「做这个款的前两组」
- 「第 N 组重出」「这版 BGM 不对」「换个参考片再跑」
- 「给新同事装环境」「一键建目录」
- 需要判断某条成片能不能交（跑质检、看抽帧证据）

## 铁律（违反任何一条都会烧钱或返工）

1. **款号先核实**。素材盘里没有就是没有，列相近款号让主管选，不改数字。
2. **花钱前要授权**。一次 US$0.373，失败不退、`no_retry`。`submit.py` 建单必须带
   `--i-have-supervisor-approval`。重出前先说明「病根 + 修法 + 花费」，让主管选。
3. **Stage A 免费、Stage B 扣费**。上传与请求体冻结可反复；建单只有一次机会。
   ~20 s 报错拿不到 task_id 时用 `--mode curl` 重发同一份 body，**不要换版本号**。
4. **场景与服装只认 Picture 1**；参考视频只借动作与时序。参考片里的门、窗、道具不写进场景句。
5. **改字不一定管用**。参考片里的污染元素（门、消防箱、栏杆、条纹背面）要动输入：
   重做参考片背景或裁掉污染区，比改提示词便宜（实测 1 次 vs 3 次额度）。
6. **骨架分镜必须人眼过**。手臂折三角、拉成直线、腿交叉 X、单腿悬空、头身分离即改 `--fixed`。
7. **机器闸门过了不等于能交**。具象道具形态要放大到 500 px 看，170 px 缩略图会误判。
8. **旧版不删**，挪进 `_记录/不合格-待重出/`；版本号在款号内连续递增，不覆盖不复用。
9. **参考先体检，且这是硬闸门**。`ref_audit.py` 出 BLOCK 就不许提交；跨款共用同一条参考必须在报告里点名——
   这是「不同款成片动作却一模一样」的头号原因，光改提示词救不回来。
   **`submit.py` 已把它实现在代码里**（缺报告/有 BLOCK/素材改过没重跑 → `exit 3`），不要绕过。

## 更新到最新（仓库分发）

技能托管在 `https://github.com/ggggoq/-skill-` 的 `minimax-sku-video/` 目录。更新就重跑这一行：

```powershell
powershell -ExecutionPolicy Bypass -Command "iwr https://raw.githubusercontent.com/ggggoq/-skill-/main/install.ps1 -OutFile $env:TEMP\i.ps1; & $env:TEMP\i.ps1 -Skill minimax-sku-video"
```

旧目录会备份成 `minimax-sku-video.bak-<时间>`；脚本只覆盖不删除，所以 `tools/ffmpeg.exe`、项目数据、你自己的配置都不会丢。

## 一次性装环境（新同事）

```
python scripts/init_project.py --root "<项目目录>" --venv --install
```

建全部目录、装引擎（`src/kling_workflow` + `tools`）、写 requirements/说明/项目标记、
下载姿态模型，最后自检 ffmpeg / 引擎 / API Key 三项，缺项明确报出来。

外部条件：
- `KLING_FFMPEG` 指向 ffmpeg.exe，或把 ffmpeg.exe 放进 `<项目>/tools/`
- `AIMAXHUG_API_KEY` 环境变量，或 `<项目>/.env`
- Python 3.10–3.12（mediapipe 限制）

## 每接一款：十步

| 步 | 命令 | 产出 / 闸门 |
|---|---|---|
| 1 | `new_sku.py --sku <款号>` | 拷素材、探真实时长、扫活动度、建交付目录、生成 `_check/<款号>/sku.config.json` |
| 2 | **`ref_audit.py --sku <款号>`** | **闸门 0（强制，0 元）**：参考是否重复、动作类型够不够、反面图有没有复制、有没有跨款共用。落 `_check/<款号>/ref_audit/report.json`；**出 BLOCK 不许进第 3 步**。姿态环境自动探测项目 `.venv`，不传 `--pose-python` 也行 |
| 3 | 填配置 | `girl / outfit / setting / setting_short / neg / sound / beats / back_crop` |
| 4 | `skeleton.py --sku <款号>` | 8 格骨架；畸形就 `--probe` 找干净帧 + `--fixed 组:秒` |
| 5 | `build_pack.py --sku <款号>` | 四图 + 4.9 s 参考切片 + manifest |
| 6 | `make_prompts.py --sku <款号>` | 提示词 + 规则检查（≤7000 字符、段 ≤1.5 s、铺满 10 s） |
| 7 | `submit.py --stage a` | 上传冻结（免费，可重来） |
| 8 | `submit.py --stage b --versions … --i-have-supervisor-approval` | 建单（扣费，一次） |
| 9 | `poll.py --download` → `finalize.py --group N --version vNNN` | 下载原片、裁 240 帧、换同窗口参考原声 |
| 10 | `qc.py --sku <款号>` | 三道闸门 + 20 格证据图 + 交付清单（含 MD5） |

每步都可加 `--project <目录>`；不加则从当前目录向上找项目标记自动定位。

### 闸门 0 是硬拦，不是提醒

`submit.py` 第 7、8 步开跑前**先读 `_check/<款号>/ref_audit/report.json`**，三种情况直接 `exit 3`，连上传都不做：

1. 报告不存在 → 没跑体检
2. 报告里有 `BLOCK`（`REF_IDENTICAL` / `REF_SAME_CONTENT` / `REF_MISSING` / `REF_UNREADABLE`）
3. 报告过期 → `01_素材库/<款号>` 里有文件比报告新（说明换了素材没重跑）

唯一放行方式：`--ignore-ref-audit "<主管原话>"`。原话与被拦项写进审批件 `_check/<款号>/approvals/*.json` 与 `_check/<款号>/ref_audit/gate.json`，事后可查。**不要为了跑通去改脚本，也不要删 report.json 糊弄——闸门只会报「没跑体检」。**

## 参考体检（第 2 步，0 元，强制）

```bash
python scripts/ref_audit.py --sku <款号>
```

一次回答四件事：四条参考是不是同一条（含改封装/裁剪）、动作类型够不够（走动/站定/抬手）、
反面图有没有被跨组复制、有没有别的款在用同一条参考。

- 结果固定落 `_check/<款号>/ref_audit/`（`report.json` + `gate.json` + 拼版图），**`submit.py` 只认这个路径**。
- `REF_IDENTICAL` / `REF_SAME_CONTENT` / `REF_MISSING` 是 BLOCK——先换参考再提交；其余是 WARN。
- 加 `--archive` 额外复制一份到 `06_处理日志/<日期>_<款号>参考核对/` 留档。
- 想连 WARN 一起卡（更严）：`--strict`。
- 判读口径与给 AI 的 6 句问法见 `references/ref-audit.md`。
- 换过素材、补过参考片以后**必须重跑**，否则闸门按「报告过期」拦。

## 脚本清单

| 脚本 | 作用 |
|---|---|
| `scripts/init_project.py` | 一键建项目骨架 + 装引擎 + 自检 |
| `scripts/new_sku.py` | 一键接款：拷素材、体检、活动度、配置模板、交付目录 |
| `scripts/ref_audit.py` | **参考体检（第 2 步，强制）**：四条参考是不是同一条、动作类型够不够、反面图有没有复制、有没有跨款共用 |
| `scripts/skeleton.py` | 8 格骨架分镜（检测版 / 固定帧版 / 候选探针） |
| `scripts/build_pack.py` | 四图 + 参考切片 + manifest |
| `scripts/make_prompts.py` | 按模板渲染提示词并跑规则检查 |
| `scripts/submit.py` | **含闸门 0**：先卡参考体检，再走 Stage A / Stage B / curl 兜底 |
| `scripts/poll.py` | 轮询任务 + 下载原片 |
| `scripts/finalize.py` | 裁 240 帧 + 换同窗口参考原声 |
| `scripts/qc.py` | 三道质检 + 证据图 + 交付清单 |
| `scripts/common.py` | 项目定位、配置、ffmpeg、API Key、时长探测（被上面共用） |

## 详细文档（按需读，别一次全读）

| 文件 | 什么时候读 |
|---|---|
| `assets/manual/环境配置手册.html` | **给新同事第一份看的东西**：只覆盖环境配置（前置准备→一键装→5 项自检→验证虚拟环境），每步配一条可直接发给 WorkBuddy 的代理提示词，双击浏览器打开 |
| `assets/manual/流程图.svg` | 需要把四阶段十步流程图贴进 PPT / 群消息 / 文档时直接用 |
| `references/pipeline.md` | 第一次接手，或某步的判定口径不确定 |
| `references/prompt-spec.md` | 写提示词 / 提示词被规则检查打回 |
| `references/ref-audit.md` | **成片动作老是那几套**：先跑参考体检，看 code 含义、阈值来源、以及丢给 AI 的 6 句问法 |
| `references/pitfalls.md` | 出现异常先按「现象」在这里检索 |
| `references/layout.md` | 不确定文件放哪、版本号怎么编 |

## 交付口径

- 成片：`02_待审核/MiniMax/<款号>/<日期>/00_直接看成片/vNNN.mp4`
- 版本对照：`02_待审核/MiniMax/<款号>/_版本对照.md`（版本 → 组 → 窗口 → 大小 → MD5 → 改了什么）
- 质检清单：`<日期>/_记录/质检清单.md` + `_记录/证据图/*_抽帧20格.jpg`
- 结论追加到 `<项目>/.workbuddy/memory/<日期>.md`

回报主管：表格（组 / 窗口 / 大小 / MD5）+ 质检数字 + 已知瑕疵（附证据图路径）+ 本批花费。
瑕疵有争议就列证据让主管定，不自己放行、也不自己偷偷重出。
