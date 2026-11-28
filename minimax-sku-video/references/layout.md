# 目录与命名

## 项目根

```
<项目>/
├── 01_素材库/<款号>/<组>/              从素材盘拷来的原始素材（不改名、不剪）
├── 02_待审核/MiniMax/<款号>/<YYYYMMDD>/
│   ├── 00_直接看成片/vNNN.mp4           交付物，主管只看这里
│   └── _记录/
│       ├── 供应商原片/vNNN_raw.mp4
│       ├── 证据图/vNNN_抽帧20格.jpg     质检证据
│       ├── 不合格-待重出/               旧版不删，挪这里
│       └── 候选/
├── 04_历史版本/<款号>/minimax-G<组>-<版本>-<日期>/
│   ├── scene_front.jpg  reverse_worn.jpg  front_flat.jpg  storyboard.jpg
│   ├── reference_video_<起>-<止>s.mp4
│   └── pack_info.json
├── 06_处理日志/<YYYYMMDD>_<款号>N组/    task_query_final_vNNN.json
├── _check/<款号>/
│   ├── sku.config.json                  ← 这一款的唯一事实来源
│   ├── activity.json                    活动度扫描
│   ├── storyboard_times.json            骨架锚点实际取值
│   └── approvals/<版本>_stage_{a,b}_approval.json
├── .minimax_bridge/
│   ├── manifests/<款号>/<版本>.json     提交闸门认的清单
│   ├── prompts/<款号>/<版本>.txt
│   └── tasks/<款号>/<版本>.json         任务记录（task_id 在这里）
├── models/pose_landmarker_lite.task
├── tools/                               check_*.py / submit_url_mode.py / ffmpeg.exe
├── src/kling_workflow/                  引擎
├── .venv/
├── .env                                 AIMAXHUG_API_KEY
└── PROJECT_README.md
```

## 命名规则

| 对象 | 规则 | 例 |
|---|---|---|
| 交付版本 | `v` + 三位连续号，**在款号内全局递增**，与组号无关，不复用不覆盖 | v001…v006 |
| 素材包 | `minimax-G<组>-<版本>-<日期>` | `minimax-G2-v005-20261116` |
| 日期目录 | `YYYYMMDD` | `20261116` |
| 抽帧证据 | `<版本>_抽帧20格.jpg` | `v003_抽帧20格.jpg` |
| 失败归档 | `<版本>_<现象>.mp4` | `v001_尾段场景跳变_木门与红箱.mp4` |

## 四条不许做的事

1. **不要给同一款号建多个日期目录**。当天出片就放当天；重出仍写进同一个日期目录，只递增版本号。
2. **不要手工改成片文件名去覆盖旧版**。旧版一律挪进 `_记录/不合格-待重出/`。
3. **不要动别人的款号目录**。跨款一律新建。
4. **不要在项目根堆一次性脚本**。要改流程就改技能里的脚本，让所有人同步受益。
