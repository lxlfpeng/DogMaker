# upcoming 切换 Sherdog 数据源 · 实施计划

> 文档：`DogMaker/plans/upcoming-sherdog.md`　版本：v0.1（2026-09-28）
> 范围：`DogMaker/ufcjson/spiders/upcoming.py`（即将到来的赛事 → `ufc_coming_data.json`）
> 目标入口：`https://www.sherdog.com/organizations/Ultimate-Fighting-Championship-UFC-2/recent-events/1` 的 **`#upcoming_tab`**（与历史赛事同页）
> 上游依据：`Resources/contract/data-files.md` §3（coming 字段契约）、《赛程页面》v3.15（§3.2 字段表 / §5.6 文案口径）、《即将到来赛程详情页》

---

## 0. 目标与非目标

**目标**：`upcoming` 从 ufc.com 切到 Sherdog，产出与 ufc.com 版**同构**的 `ufc_coming_data.json`（字段契约不破），并让 App 的《赛程页面》/《即将到来赛程详情页》在 Sherdog 数据源下可用。

**非目标**：`ranking` / `athlete` 仍未切换（另立计划）；端上不改（App 的 `name` 内置映射表也不扩充——本爬虫**反向适配**它，见 §5 D4）。

---

## 1. 背景与硬约束

1. **契约**（`data-files.md` §3）：`name` 是赛事英文标识（如 `UFCFightNight` / `UFC333`）；`title` 未定阵写 `TBD vs TBD`；`main_time` 是 **Unix 秒字符串**；`fight_card[]` 可为空数组；卡上带 `card_type` / `fight_name` / `card_division` / `red_rank` / `blue_rank` / `fight_id` 等。
2. **App《赛程页面》§5.6**：`name` 有**内置映射**（精确表 `UFCFightNight` → 「UFC 格斗之夜」+ 正则 `^UFC(\d+)$` → 「UFC N」，未命中按原文）；头条大图= `banner_local`，双方头像/中文名靠 `fight_card[0]` 的 `red_page`/`blue_page` 与**本地 `player.page` 精确匹配**；倒计时目标 = `main_time`，头条日期展示**到分钟**。
3. **DogMaker 同构要求**：与 UfcMaker 同名同路径的 7 个 JSON + `ufc.db.zip`（13 张表）+ `output/images/` 同相对路径。

---

## 2. 已实测页面结构（取证 2026-09-28，选择器可直接落地）

### 2.1 列表页（`#upcoming_tab`，与 `#recent_tab` 同页）

- 实测 **9 场**（2026-10-03 ~ 2026-12-12），一页全见、**无分页**。
- 行结构与历史赛事完全相同：`tr[onclick]` + `meta[itemprop=startDate]` + `a[itemprop=url]` + `td[itemprop=location]`。
- ⚠️ 只有「日期 / 赛事名 / 地点」三项，**日期只到日**（`2026-10-03T00:00:00+00:00`）。

### 2.2 赛事详情页（未开打形态）

- 头条主赛 = `div.fight_card`：选手角标是 `span.final_result yet_to_come`（无结果）；
- 其余对局 = `table.new_table.upcoming` 的 `tr[itemprop=subEvent]`，列 = **Match / 左选手 / 量级 / 右选手 / 空**
  （**无方式、回合、时间、赔率**；`final_result` 在表格行里被注释掉）；
- ⚠️ 未定阵形态：`UFC 335 - TBA` 实测**只有 hero**（双方已定、对手未定）；完全未定阵的赛事整页可能无战卡；
- ⚠️ 「双方未定」的对局，href 是 **`javascript:void();`**（实测 UFC Qatar 页）——必须挡成空串，否则 `scrapy.Request` 抛 `Missing scheme` 并让**整场 item 丢失**；
- **无主/副/早卡分区标记**（与历史赛事同结论）。

### 2.3 选手页

与 eventpass 完全同一页 → 共用 `ufcjson/sherdog.py` 的 `build_player_item()`（唯一实现）。

### 2.4 相对 ufc.com 的数据缺口

| 缺口 | 影响面 | 处置 |
|---|---|---|
| **开赛时刻**（只有日期） | `main_time` = 当天 00:00 UTC，不是真实开赛时刻 → App 倒计时提前归零、「到分钟」的日期偏早 1–3 小时 | 落真值、不猜时刻；**登记为源站缺口**（§5 D2） |
| 卡位分区 | `card_type` 无真值 | 全量 `Main`（与 eventpass 同一决策 D1） |
| 赔率 / 排名 / `fight_id` | 空串 | App 端：赔率空不展示、排名空不显示框（《赛程页面》§5.3/§5.6）、`fight_id` 允许空串（契约） |
| 赛事封面 | 只有 `image_vs`（200×100，过小） | `banner`/`banner_local` 留空 → 头条大图走**深色纯色底 + 渐变**（§7.3/§8.3） |

---

## 3. 字段映射（Sherdog → `ufc_coming_data.json`）

| JSON 字段 | 来源 | 规则 |
|---|---|---|
| `name` | 列表名拆分后的**头部** | `map_coming_name`：`UFC <n>` → `UFC<n>`；含 `Fight Night` → `UFCFightNight`；其余原文（对齐 App §5.6 内置映射） |
| `title` | 列表名拆分后的**尾部** | `normalize_vs`：`vs.` → `vs`；尾段仅 `TBA`/`TBD` → `TBD vs TBD`（对齐 ufc.com 口径） |
| `page` | 列表行链接 | 绝对化（`https://www.sherdog.com` + href） |
| `main_time` | `meta[itemprop=startDate]` | ISO → Unix 秒字符串（UTC 00:00） |
| `prelims_time` / `data_early_time` | — | 空串 |
| `address` | 列表地点行 | `parse_location` 去场馆段 → `City,State,Country`（App 按首段=城市/末段=国家消费） |
| `banner` / `banner_local` | — | 空串（见 §2.4） |
| `fight_card[]` | hero + `table.new_table.upcoming` 行 | **顺序 = 头条主赛先**（契约「第一条即头条主赛」） |
| └ `card_type` | — | `Main`（全量） |
| └ `fight_name` | 赛事 `name` | 与赛事级一致 |
| └ `card_division` | `span.weight_class`（+ hero `span.title_fight`） | `<W> Bout` / `<W> Title Bout`；`Strawweight` → `Women's Strawweight`；空 → 空串 |
| └ `red_page` / `blue_page` | 左 / 右选手 | 绝对化；`javascript:void();` → **空串**（未定选手，App 走 TBD） |
| └ `red_odds` / `blue_odds` | — | 空串 |
| └ `red_rank` / `blue_rank` | — | 空串 |
| └ `fight_id` | — | 空串 |
| └ `main_time` / `address` | 赛事级字段 | 原样带在卡上（契约要求） |

---

## 4. 代码改动清单

| 文件 | 改动 |
|---|---|
| `ufcjson/sherdog.py` | **新增**：Sherdog 解析共用层（纯函数 + 选手页 `build_player_item`），eventpass / upcoming 共用 |
| `ufcjson/spiders/upcoming.py` | **整文件重写**（Sherdog）：`#upcoming_tab` → 详情 hero + `table.new_table.upcoming` → `UfcComingItem`（内嵌卡列表） |
| `ufcjson/spiders/eventpass.py` | 瘦身：已抽出的解析件改为 `from ..sherdog import …`；行为不变（回归见 §7） |
| `ReadMe.md` | 数据来源、命令、结构行更新 |
| `plans/upcoming-sherdog.md` | 本文件 |

### 4.1 与 ufc.com 版的有意差异

1. **本爬虫会抓战卡双方选手页并写 `player` 表** —— ufc.com 版不抓（靠 `athlete` 爬虫兜）。原因：Sherdog 的卡上 `red_page`/`blue_page` 是 Sherdog 选手 URL，而 `athlete` 已于 **2026-09-30 移除** ⇒ 不自己补齐，App 赛程页/详情页会「查不到选手」而整片 TBD + 占位头像。成本：每轮约 200+ 请求（9 场 × ~28 人），与 `eventpass` 同一套礼仪。
2. `name` **必须归一**成 App 能识别的标识（否则眉标退化为英文长串，见 §5 D4）。
3. 无 skip 逻辑（ufc.com 版也没有）：每次全量重出 9 场，JSON 覆盖写。

---

## 5. 决策项

| # | 事项 | 结论 | 说明 |
|---|---|---|---|
| D1 | 无分区数据 → `card_type` | ✅ 全量 `Main` | 与 eventpass 同一拍板（App 端档位 tab 自然隐藏） |
| D2 | `main_time` 只有日期 | ✅ 按 UTC 00:00 落库 + 登记偏差 | 不猜开赛时刻；产品若要精确倒计时需另找数据源 |
| D3 | 赛事封面 | ✅ 留空 | App 头条大图走深色底 + 渐变（§7.3/§8.3） |
| D4 | `name` 归一为 `UFCFightNight` / `UFC<n>` | ✅ 采纳 | 对齐 App §5.6 内置映射；未命中规则的（Road to UFC 等）按 Sherdog 原文 |
| D5 | 选手页一并抓（写 `player`） | ✅ 采纳 | 见 §4.1-1（否则 App 侧整片 TBD） |

---

## 6. 实施步骤与出口判据

| 步骤 | 内容 | 出口判据 | 状态 |
|---|---|---|---|
| **S1** 共用层抽取 | 新增 `ufcjson/sherdog.py`，`eventpass` 改为引用 | 快照回归全绿（行为不变） | ✅ 2026-09-28 |
| **S2** 重写 `upcoming.py` | 列表 + 详情 + 选手三处解析 | 快照全绿（见 §7） | ✅ 2026-09-28 |
| **S3** 实弹跑 | `scrapy crawl upcoming` | 9 场全部出 JSON；player 行补齐；0 结构异常 | ✅ 2026-09-28（数据见 §7 末行） |
| **S4** 文档与提交 | ReadMe + 本文件；提交 | 仓库干净 | ⏸ |

---

## 7. 验证证据（2026-09-28，HTML 快照 + 实弹）

- **列表**：9 场；`name` 映射实测 `UFC332` / `UFCFightNight` / `UFC333` / `UFC Qatar` / `UFC335`；两个 TBA 赛事 → `title = TBD vs TBD`；`address` 去场馆（`Salt Lake City,Utah,United States`）；
- **UFC 332 详情**：14 张卡（hero + 13 行）；首张 = `Natalia Silva vs Cong Wang`，`Flyweight Title Bout`（源站 hero 带 `TITLE FIGHT` 标记）；卡上赛事级字段齐全；
- **UFC 335（只有 hero）**：1 张卡（Moreno vs Johnson）；
- **UFC Qatar**：4 张卡；未定选手的 `javascript:void();` 被挡成空串（**修掉了会让整场崩掉的 bug**）；
- **eventpass 回归**（重构后）：列表判重只派发刷新窗口内的赛事；UFC 331 = 12 张卡、hero `Flyweight Title Bout` / `Decision - Unanimous` / R5；Van 选手页 `18-2-0 (W-L-D)` / `65.00` / `125.00` / 24 条历史 —— 与重构前逐项一致；
- **实弹**（2026-09-28）：`scrapy crawl upcoming` 全量跑完（`finished`，0 结构异常告警）——
  **9 场全部出卡：共 89 张**（Qatar 4 / UFC332 14 / UFC333 11 / UFC334 12 / UFC335 1 / FN 10·12·12·13）；
  `ufc_coming_data.json` 已由 Sherdog 数据覆盖（`name` 归一为 `UFC332`/`UFCFightNight`/`UFC335`…、
  两场未定阵 `title = TBD vs TBD`、`banner`/`banner_local` 空、赔率/排名空串）；
  卡上双方选手行写入 `player` 表（本轮后 `player` 共 914 行、全部为 Sherdog 键）。

---

## 8. 风险与对策

| 风险 | 对策 |
|---|---|
| Sherdog 限流 | 与 `eventpass` 同口径（1.5s 延迟 + 单域串行 + 429/403 重试） |
| 赛事名格式未穷举 | 未命中规则时按 Sherdog 原文（App 原样展示，可接受）；如遇新形态补 `map_coming_name` |
| 开赛时刻缺失 | 已登记（§2.4）；如需精确倒计时，建议评审时考虑「用其它源补时刻」而不是端上推算 |
| 选手页抓取量 | 每轮 ~200+ 请求；后续若接 `athlete` 切换，可评估是否只抓头条双方以省请求 |