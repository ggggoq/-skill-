# 提示词规格

模板：`assets/templates/prompt.txt`。字段由 `sku.config.json` 提供，改字段不改模板。
硬上限 7000 字符，机器复核用 `<项目>/tools/check_prompt_rules.py`。

## 结构（七段，顺序固定）

| 段 | 作用 | 关键点 |
|---|---|---|
| `subject_definitions` | 编号声明 Picture / Video / Audio 各是什么 | `<Subject 4>` 必须写明「只借动作，身份/服装/场景/构图全部丢弃」 |
| `summary` | 一句话说清这是 10 秒竖屏复刻 | 明写「从第一帧到 00:04.9 复刻，之后按下面的节拍继续」 |
| `retention_analysis` | 每个输入保留到什么程度 | Picture 4 只保留「姿态」，颜色已去掉 |
| `detailed_description` 开头 | 机位与画幅契约 | 手持、无推拉摇移、全身到鞋、**她不会在画框里变大** |
| 节拍列表 | 逐段写明动作 | 每段 ≤1.5 s，首尾相接，秒数带毫秒 |
| 服装锁定句 | 防漂色/防串版 | 「front and back the same, no stripes, no fade…」+ 刺绣只在某一条腿上 |
| 场景锁定句 | 防场景跳变 | 点名场景要素 + 按组点名的排除项 + 「不换景不移动」 |
| `overall_soundscape` | 环境声 | 写具体声音，不写音乐；`non_diegetic_music: N/A` |

## 五条最容易犯的错

1. **一段塞两个动作。** `She turns and then raises both hands and smiles` 这种 3 秒一段的写法会让模型只做前半段。
   拆成 1.0–1.5 s 的小段，每段一个动作。
2. **含糊停顿。** `holds for a moment` / `pauses briefly` 都不算节拍，规则检查会挂。写成
   `holds them there until 00:03.400`。
3. **场景写给参考片。** 场景句只能描述 Picture 1。参考片里的门、窗、消防箱、栏杆出现在提示词里，
   模型就会照搬（哪怕加了否定）。
4. **否定粒度太粗。** `no doorway` 会把 Picture 1 里本来就有的木门一起否掉，画面会闪。
   否定要写「这组画面里没有、但模型可能会加」的东西：`no glass door, no shop front, no street, no second wall`。
5. **漏写构图契约。** 只写 `walks toward the camera` 而不写 `she never grows in the frame, head to the soles of her shoes from the first frame to the last`，
   模型会做成推镜 + 半身。

## 写法示例

节拍（好）：

```
00:02.400-00:02.900 She stands with her back to the lens, feet planted, a small settling of her shoulders.
00:02.900-00:03.900 She comes back round the same way to half profile, arms hanging.
00:03.900-00:04.900 She finishes the turn to face the lens, her feet staying on the same paving stones.
```

节拍（差）：

```
00:02.400-00:04.900 She turns around and comes back, then smiles for a moment.
```

服装锁定（好）：

```
The top stays one even dusty pink, front and back the same, plain and smooth, with no stripes,
no ribbing across the body, no print and no shine. The trousers stay one solid black from waist
to hem, and the white embroidery stays thin and small on her left thigh only.
```

场景锁定（好）：

```
The setting is the arch pilaster, the beige wall, the dark timber gate, the white pebbles and the
grey paving, and it stays the same for the whole ten seconds: no glass door, no shop front,
no street, no second wall, no second person anywhere in the frame at any moment.
```

## 长度控制

7000 字符不算紧，但四条腿（人物 + 服装 + 场景 + 10 段节拍）一起写很容易超。压缩顺序：

1. 先删重复的形容词（`soft`, `very`, `slightly` 这类无信息量的词）。
2. 合并同类锁句（服装色/材质/图案写一句，不要分三段）。
3. 最后才缩短场景描写——但**不能删掉场景要素的逐项枚举**，那是防跳变的主力。
