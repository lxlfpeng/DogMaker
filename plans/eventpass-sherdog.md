# eventpass 切换 Sherdog 数据源 · 实施计划

> 文档：`DogMaker/plans/eventpass-sherdog.md`　版本：v0.2（2026-09-27 决策已拍板、S1/S2 已实施）
> 范围：`DogMaker/ufcjson/spiders/eventpass.py`（历史赛事战报：列表 + 详情 + 随行选手页）
> 目标入口：`https://www.sherdog.com/organizations/Ultimate-Fighting-Championship-UFC-2/recent-events/1`
> 上游依据：《数据来源及图片地址》§1.1（数据源前缀）、`MMABoxDocs/contract/data-files.md` §1（同构要求）、`MMABoxDocs/contract/db-schema.md`（表结构契约）、《历史赛事页面》v1.20

---

## 0. 目标与非目标

**目标**：把 DogMaker 的 `eventpass` 爬虫从 ufc.com 切到 Sherdog 的 UFC「Recent Events」列表，在**不改 App 数据契约**（`ufc.db` 表结构与字段语义不变）的前提下产出 `pass_event` / `pass_card`，并随行补齐对局涉及选手的 `player` 行。

**非目标（本期不做，另立计划）**：
- `athlete` 爬虫已**移除**（2026-09-30，含其独有的 `birth_place.py`；见 §7 D5）；`ufccn_news` 与 RSS 管线/产物也**已移除**（RSS 与新闻由 UfcMaker 负责，App 的 RSS 地址固定走默认源）；
- `player` 表的**名册覆盖**（没上过战卡的选手）——事件驱动只能覆盖「出现在战卡/榜单上的人」，如要补需另立「Sherdog 名册枚举」计划（见 §7 D5）；
- 端上任何改动（App 只在「数据源」前缀切换后整体换源，不做单表混用）。

---

## 1. 背景与硬约束

1. **DogMaker 的定位**：App「设置 → 数据源」切换的 Sherdog 侧仓库，发布前缀 `/lxlfpeng/DogMaker/refs/heads/main/` 已写入 `config.json` 的 `data_sources[1].prefix`。
2. **同构硬约束**（`data-files.md` §1）：DogMaker 必须与 UfcMaker **同名同路径的 7 个 JSON + `output/db/ufc.db.zip`（13 张表 schema 一致）+ `output/images/` 同相对路径**，否则切源后页面数据全空、数据库更新直接崩、图片全落占位图。
3. **现状**：DogMaker 源码是 UfcMaker 的 1:1 副本（全部未跟踪），`eventpass` 仍抓 `https://www.ufc.com/events?page=N`；ufc.com 对大陆 IP 整站 404，Sherdog 可直连（`-L` + 常规 UA 实测 200）。
4. **App 消费口径不可动**（《历史赛事页面》§3.3/§7.5）：
   - `pass_card.fight_page` → `pass_event.page`、`red_page`/`blue_page` → `player.page` 均为**完整 URL 精确匹配**；
   - 同一场赛事内 **`id` 升序 = 卡序，头条主赛排最前**（`id` 最小 = 头条主赛，第 2 行 = 联合主赛）；
   - `card_type` 只认 `Main` / `Prelims` / `EarlyPrelims`；`card_division` 中**含「冠军」**即渲染冠军战徽章；
   - `end_method` / `card_division` 由翻译链路出 `*_cn`（缓存优先，见 §5.3）。

---

## 2. 已实测的 Sherdog 页面结构（取证 2026-09-27，选择器可直接落地）

### 2.1 列表页（Recent Events）

- URL 模板：`/organizations/Ultimate-Fighting-Championship-UFC-2/recent-events/{page}`，**100 条/页**。
  - 实测 page 1：2026-09-26 → 2024-08-23；page 2：2024-08-17 → 2022-08-06。
  - 组织页文案口径：`has held 823 events and presided over approximately 9,064 matches` → 全量约 **9 页**。
- ⚠️ **必须锁定 `div.single_tab#recent_tab` 下的 `table.new_table.event`**：同页还有 `#upcoming_tab`（9 条未来赛事，表格结构完全相同）——不锁定会把未开打的赛事当已完结混入。
- 行结构（`tr[onclick]`，`itemscope itemtype="...Event"`）：

| 取值 | 选择器 |
|---|---|
| 日期 | `meta[itemprop="startDate"]/@content`（ISO 带时区，如 `2026-09-19T00:00:00+00:00`） |
| 赛事名 | `a[itemprop="url"] → span[itemprop="name"]`（如 `UFC 331 - Van vs. Pantoja 2`） |
| URL | `a[itemprop="url"]/@href`（相对路径 `/events/<slug>-<id>`） |
| 地点 | `td[itemprop="location"]`（旗帜 `img` + 文本，如 `Crypto.com Arena, Los Angeles, California, United States`） |

- 翻页：表尾 `span.pagination` 内有 `a`（文案 `Older Events »`）则继续；该链接缺席（仅 `<span></span>` 占位）= **最后一页**。
- 列表页只有「日期 / 赛事名 / 地点」三项，战卡与结果全在详情页。
- 实测 100/100 行均为 `/events/UFC-…`（该组织页不含 PFL / Contender Series 等他组织赛事）。

### 2.2 赛事详情页（`/events/<slug>-<id>`）

- 头部 `div.event_detail`：`h1 span[itemprop="name"]`、`meta[itemprop="startDate"]`、`span[itemprop="location"]`。
- **头条主赛 hero**：`div.fight_card`
  - 双方：`.fighter.left_side` / `.fighter.right_side`，各含 `a[itemprop="url"]`（→ `/fighter/…`）+ `span[itemprop="name"]` + `span.final_result`（`win`/`loss`/…）；
  - 中部：`b`（`MAIN EVENT`）、`span.title_fight`（**冠军战标记**，有值即 `TITLE FIGHT`）、`span.weight_class`；
  - 下方 `table.fight_card_resume`：`Method` / `Referee` / `Round` / `Time`。
- **其余对局**：`table.new_table.result > tr[itemprop="subEvent"]`
  - 场次序号（Match）、左右 fighter（`div.fighter_list.left/.right`，含 `a[itemprop="url"]` + `span.final_result`）、`span.weight_class`、`td.winby > b`（结束方式）、`R`、`Time` 三列。
- **排序**：hero = 头条主赛（当晚最后一场）；表格按 **Match 号降序**（第 1 行 = 联合主赛，最后一行 = 当晚第一场）。
- ⚠️ **无主赛/副赛/早场分区标记**：UFC 300 / 325 / 331 / 332 实测均无 `Prelim` 字样，hero 也只有一个 `MAIN EVENT` 标签。
- ⚠️ **无赔率**；事件图仅 `image_vs/<id>`（实测 200×100，too small 给 353×216 封面槽）不可作封面 —— 2026-10-02 改判：由头条双方头像拼接生成（§7 D3）。
- 结果时效实测：FN 289（2026-09-26 当晚赛事，09-27 抓取）11 场对局**结果/方式/回合/时间已全部就绪**。

### 2.3 选手页（`/fighter/<Name>-<id>`）

| 字段 | 选择器 / 位置 | 实测样例 |
|---|---|---|
| 姓名 | `h1[itemprop="name"] span.fn` | `Joshua Van` |
| 昵称 | `span.nickname`（原文带引号） | `"The Fearless"` |
| 国籍 | `strong[itemprop="nationality"]` | `Myanmar` |
| 出生地 | `span[itemprop="addressLocality"]` | `Hakha, Chin` |
| 旗帜 | `/img/flags/big/<2 位码>.png` | `mm.png` |
| 照片 | `/image_crop/200/300/_images/fighter/….JPG` | `https://www.sherdog.com/...` 实测 200 `image/jpeg` |
| 生日 | `span[itemprop="birthDate"]` | `Oct 10, 2001` |
| 身高 | bio 表 `HEIGHT` | `5'5"` / `165.1 cm` |
| 体重 | bio 表 `WEIGHT` | `125 lbs` / `56.7 kg` |
| 团队 | `ASSOCIATION` | `4oz. Fight Club` |
| 量级 | `CLASS` | `Flyweight` |
| 胜场分布 | 三个 meter：`KO/TKO`、`SUBMISSIONS`、`DECISIONS` | `9 / 2 / 7` |
| 战绩 | `winsloses-holder` 三数 | Wins 18 / Losses 2 / Draws 0 |
| 历史 | `FIGHT HISTORY - PRO` 表（Result / 对手 / 赛事+日期 / 方式 / R / Time） | 含非 UFC 职业场次 |

### 2.4 相对 ufc.com 口径的数据缺口

| 缺口 | 影响面 | 处置 |
|---|---|---|
| 卡位分区（主/副/早卡） | `card_type` 无真值 | §7 D1（已拍板：全量 `Main`） |
| 赔率 | `red_odds`/`blue_odds` 空 | App 侧空/`-` 不展示（PRD §7.5-4）→ 可接受 |
| 赛事封面 | `banner` 无合适图（`image_vs` 200×100） | §7 D3（**2026-10-02 改判**：头条双方头像拼接 → `banner_local`；拼不出的少数仍走 App 深色底降级） |
| reach / leg_reach / style / status / cover / 性别 | `player` 对应列为空 | App 各自降级/隐藏（PRD §8.1、选手详情页 §8） |
| 女子量级前缀 | `Flyweight` 等不区分男女 | §7 D2（已拍板：仅 Strawweight 加前缀） |
| **老赛事量级** | 约 2009 及更早的赛事，Sherdog 的 `span.weight_class` **存在但为空**（实证 UFC 103）→ 全量回填约 **1,550** 场对局 `card_division` 为空 | 留空不猜（App 端场次行只显示场次标）；如需补，后续可从 ufc.com 库交叉回填（UfcMaker 侧旧数据对老赛事有量级） |
| **源站无对局** | 全量回填实测 4 场：`UFC 151`/`UFC 176`（取消赛事）、`UFC 233`/`FN 97`（Sherdog 页无战卡数据）→ 该期 `pass_event` 有行、`pass_card` 0 行 | 保留赛事行（App §8.1 降级为「待定占位」），爬虫已打 warning 留痕 |

---

## 3. 字段映射（Sherdog → `ufc.db`）

### 3.1 `pass_event`（13 列 → **2026-10-02 恢复地点四列，17 列**）

| 列 | 来源 | 转换规则 |
|---|---|---|
| `page` | 列表 `a[itemprop="url"]/@href` | 绝对化 = `https://www.sherdog.com` + href（唯一键、关联键） |
| `name` | 列表 `span[itemprop="name"]` | 以 `" - "` 拆分**取前半**：`UFC 331` / `UFC Fight Night 289` / `UFC`（Road to UFC 类无 `vs.` 时整串作 name） |
| `title` | 同上 | `" - "` 后半：`Van vs. Pantoja 2` / `Rosas Jr. vs. Barcelos`；无 `vs.` 时留空 |
| `address` | `td[itemprop="location"]` | 去旗帜 `img` 与空白 → 拆段去场馆（规则见下）→ 国家别名归一 |
| `city` / `country` | `address` 拆段 | 首段=城市、末段=国家（单段=国家）——**2026-10-02 恢复写入**；`city_cn` / `country_cn` 走翻译链路 |
| `main_time` | `meta[itemprop="startDate"]` | ISO → **Unix 秒字符串**（UTC 00:00 → 如 `1789776000`） |
| `prelims_time` / `data_early_time` | — | 空串（Sherdog 无卡段时间） |
| `banner` / `banner_local` | 拼图产物 | `banner` 空串（源站无海报）；`banner_local` = `full/<sha1("banner\|"+page)>.webp`（ufcjson/banner.py 用头条双方头像拼接，2 倍图 706×432；头像缺失则留空 → App 深色底降级） |
| `name_cn` / `title_cn` / `address_cn` | 翻译链路 | 不变（跑完后统一补翻） |

**地址拆段规则**（对齐 ufc.com 库内形态 `City,State,Country` / `City,Country`，App 端「第一段=城市、末段=国家」依赖它）：

1. **≥4 段**（`Venue, City, Region, Country`）→ 丢弃首段（场馆）→ `City, Region, Country`；
2. **3 段**（`Venue, City, Country`）→ 首段命中场馆关键词（`Arena|Apex|Center|Centre|Stadium|Garden|Hall|Dome|Coliseum|Casino|Theatre|Theater|Live|O2|Park|Field`）才丢，否则原样保留；
3. **≤2 段** → 原样；
4. 国家段别名归一：`England / Scotland / Wales → United Kingdom`（与库内 ufc.com 口径一致，如 `London,United Kingdom`）。

> 实证：page 1 共 100 行 —— 79 行为 4 段、21 行为 3 段，**全部带场馆**（如 `UFC Apex, Las Vegas, Nevada, United States`、`Accor Arena, Paris, France`）。

### 3.2 `pass_card`（15 列契约）

| 列 | 来源 | 转换规则 |
|---|---|---|
| `fight_page` | 详情页 URL | = `pass_event.page` |
| `red_page` | **左侧** fighter `a[itemprop="url"]` | 绝对化；角位约定「Sherdog 左侧 → red_page」在历史审计 140/140 吻合 |
| `blue_page` | **右侧** fighter `a[itemprop="url"]` | 同上 |
| `red_result` / `blue_result` | `span.final_result` | `win→Win`、`loss→Loss`、`draw→Draw`、`nc→NC` |
| `card_division` | `span.weight_class`（+ hero `span.title_fight`） | `<Weight> Bout`；有 `TITLE FIGHT` → `<Weight> Title Bout`；`Strawweight` → `Women's Strawweight Bout` |
| `card_type` | — | 本期统一 `Main`（§7 D1） |
| `end_method` | 表格 `td.winby > b` / hero `resume` 的 `Method` | 归一映射（见下），目标 = **现有翻译缓存已覆盖的词表** |
| `end_round` / `end_time` | 表格 `R` / `Time`（hero 从 `fight_card_resume`） | 原样（`3` / `5:00`） |
| `red_odds` / `blue_odds` | — | 空串 |
| **写入顺序（关键）** | hero 先，其后表格行序（Match 降序） | 保证 **`id` 升序 = 头条主赛最前**、第 2 行 = 联合主赛（App §3.3/§7.5 的场次标与卡序依赖） |

**`end_method` 归一映射**（Sherdog 文本 → 库内口径 → 缓存译文）：

| Sherdog 实测样例 | 归一目标 | 缓存状态 |
|---|---|---|
| `KO (Punches)` / `KO (Elbow)` / `TKO (Punches)` / `TKO (Front Kick to the Body and Punches)` | `KO/TKO` | ✅ 击倒/技术性击倒 |
| `TKO (Doctor Stoppage)` | `TKO - Doctor's Stoppage` | ✅ TKO-医生叫停 |
| `Submission (Rear-Naked Choke)` / `Submission (Guillotine Choke)` | `Submission` | ✅ 降服 |
| `Decision (Unanimous)` / `(Split)` / `(Majority)` | `Decision - Unanimous` / `- Split` / `- Majority` | ✅ 一致/分歧/多数判定 |
| `Disqualification (Illegal Knee)` | `DQ` | ✅ 取消资格 |
| `No Contest` | `No Contest` | ✅ 无结果 |
| `Draw` | `DRAW` | ✅ 平局 |
| 其余（`Other` 兜底） | 原样透传 | ✅ 其他（未命中走 LLM，量小） |

### 3.3 `player`（eventpass 随行产出，够用为度）

| 列 | 来源 | 转换规则 |
|---|---|---|
| `name` | `h1 span.fn` | — |
| `nick_name` | `span.nickname` | 去首尾引号 |
| `page` | `/fighter/<Name>-<id>` | 绝对化（唯一键；⚠️ 与 ufc.com 行不同键，见 §7 D5） |
| `avatar` | portrait `200×300` | 绝对化；取不到 → **空串**（不注入 ufc.com 占位图，见 §7 D7） |
| `cover` | — | 空串（App 用人像位降级） |
| `record` | Wins/Losses/Draws 三数 | `18-2-0 (W-L-D)`（与库内格式一致） |
| `division` | `CLASS` | `<X> Division`；`Strawweight` → `Women's Strawweight Division`（与库内 `player.division` 口径一致，「Division」后缀可被译文剥掉） |
| `home_town` / `city` / `country` | `nationality` + `addressLocality` | `home_town = "<locality 首段>, <nationality>"`；`city` = locality 首段；`country` = nationality |
| `flag` | 旗帜 URL 的 2 位码 | 用 ISO 码查 `pycountry` → emoji（比按国家名模糊匹配稳） |
| `height` | `5'5"` | **英寸**、2 位小数 → `65.00`（与库内口径一致） |
| `weight` | `125 lbs` | **磅**、2 位小数 → `125.00` |
| `wins_stats` | 三个 meter | `[{"way":"Wins by Knockout","times":"9"},{"way":"Wins by Submission",…},{"way":"Wins by Decision",…}]`（三条译文缓存已齐） |
| `history` | `FIGHT HISTORY - PRO` 行 | 合成与库内风格一致的英文叙述行，如 `(9/19/26) Van won a five round unanimous decision over Alexandre Pantoja`（新文本由翻译链路补译） |
| `birthdate` | `span[itemprop="birthDate"]` | `Oct 10, 2001` → `2001-10-10` |
| `reach` / `leg_reach` / `style` / `status` / `debut` | — | 空串（缺口已登记 §2.4） |

---

## 4. 代码改动清单

| 文件 | 改动 |
|---|---|
| `ufcjson/spiders/eventpass.py` | **整文件重写**（见 §4.1）；删除 ufc.com 专用逻辑：`normalize_athlete_url` / `extract_avatar` / `parse_address` 及 `athlete_url` import |
| `ufcjson/sherdog.py` | **新增（2026-09-28）**：Sherdog 解析共用层——纯函数（名称/地址/时间/量级/方式映射、身高体重生日换算、历史造句）+ 选手页 `build_player_item()`；`eventpass` 与 `upcoming` 共用同一实现，防两套解析漂移（见 `plans/upcoming-sherdog.md` §4） |
| `ufcjson/settings.py` | **不改**：礼仪与重试落在 spider 的 `custom_settings`（只影响 eventpass，不波及仍走 ufc.com 的其它爬虫） |
| `ufcjson/pipelines/image.py` | `UfcDefaultPhotoPipeline`：不再注入 ufc.com 占位图（缺失 → 留空，App 用「加载失败默认图」降级） |
| `ufcjson/pipelines/export_db.py` | `pass_event` 建表 DDL：2026-09-30 曾去掉 4 个历史列（→13 列）；**2026-10-02 用户拍板恢复**（`city` / `country` 爬虫拆段写入、`_cn` 翻译回填；存量库 `scripts/backfill_event_place.py`） |
| `run.py` | **2026-09-30 更新**：ufc.com 时代的两步收尾（`normalize_db()` 多行归一 / `reconcile_pass_card()` URL 对账）已随 `normalize.py`、`athlete_url.py` 一并**移除**——Sherdog 数据下它们本就是 no-op；收尾只剩「翻译 + 图片维护」。契约要求的两张辅助表（`player_url_alias` / `player_url_probe`）改由 `export_db.py` 的 player 分支建表，库中保持空表 |
| `ReadMe.md` | 数据来源说明、抓取命令、爬虫行为更新 |
| `plans/eventpass-sherdog.md` | 本文件（评审后按结论回填 §7） |

### 4.1 重写后的 `eventpass.py` 结构

```text
常量:
  LIST_URL_TEMPLATE = ".../recent-events/{page}"
  MAX_PAGES = 20                      # 防御上限（实际约 9 页）
  VENUE_KEYWORDS / COUNTRY_ALIASES / METHOD_MAP / EVENT_SPLIT

生命周期:
  __init__(pagination=False)          # 去掉 normalize_urls 参数
  start_requests()                    # 从 recent-events/1 起
  closed()                            # 沿用现有统计 + 未匹配项 warning

解析:
  parse(response)                     # 列表页：锁定 #recent_tab → 行 → skip/refresh 判定 → 详情请求
  parse_detail(response)              # 头部 + hero + result 表格 → UfcPassItem / UfcPassCardItem
  parse_fighter(response)             # 选手页 → UfcPlayerItem

纯函数（便于单测）:
  parse_event_name(full) -> (name, title)
  parse_location(raw) -> address
  iso_to_unix(iso) -> str
  map_method(raw) -> str
  map_division(weight_class, is_title) -> str
  parse_record(w, l, d) -> "18-2-0 (W-L-D)"
  parse_height / parse_weight / parse_birthdate
  build_history(rows) -> [str]
```

- **保留**：按 `page` 跳过已存在赛事、`pagination` 全量开关、结束时输出统计与结构变化 warning。
- **新增（防残缺）**：**结果刷新窗口** —— 列表中 `startDate` 落在最近 N 天（默认 7 天）的赛事，即使已在库中也重抓详情；`SqliteDbPipeline` 对 `pass_card` 的 UPDATE 分支只覆盖非空值，重抓安全（防「赛果未出时入库、之后永不刷新」）。

---

## 5. 迁移与运行

### 5.1 迁移（一次性）

```sql
-- 清行不清表：保留 17 列表结构与 player_url_alias / player_url_probe
DELETE FROM pass_event;
DELETE FROM pass_card;
```

> 若将来从空库重建：`export_db.py` 的 DDL 已含地点四列（2026-10-02 恢复 → 17 列），空库重建会直接产出 17 列。

### 5.2 全量回填

```bash
scrapy crawl eventpass -a pagination=true     # ≈9 页列表 + ≈823 详情页 + ≈1,800 选手页
```

- Scrapy 去重（同一 run 内）保证选手页按唯一 URL 只取一次；
- 1.5s 延迟 + 串行 ≈ 1.5–2 小时；建议先按「前 10 场」小样试跑核对字段，再全量。

### 5.3 常态运行（run.py 周天，不变）

- 第 1 页 100 条覆盖近 2 年 → 每周新增赛事必在其内，只抓新增（+ 刷新窗口内的重抓）；
- 收尾链：翻译（缓存优先）→ 图片维护 → `db.zip` → `meta.json`（ufc.com 时代的归一化/对账两步已于 2026-09-30 移除）。

---

## 6. 其余确认（已闭环，供实施时直接采用）

- `UfcPassItem` / `UfcPassCardItem` / `UfcPlayerItem` 字段**无需新增**（`url` 字段即 `pass_event.page`）；
- 图片下载通道可用：`MEDIA_ALLOW_REDIRECTS = True` 已开（Sherdog 图片多经 301 跳转）；`avatar_local` 落 `full/<sha1>.webp`；
- `allowed_domains` 需含图片域：`www.sherdog.com` + `www1~4-cdn.sherdog.com`；
- `player` 插入要求 `name`/`page`/`division`/`avatar`/`cover` 五个键**必须存在**（导出管道硬下标）→ 抓不到也要写空串。

---

## 7. 决策项（2026-09-27 已拍板）

| # | 事项 | 结论 | 备选 / 影响 |
|---|---|---|---|
| D1 | 无卡位分区数据，`card_type` 怎么填 | ✅ **已拍板：全部 `Main`**（用户 2026-09-27 确认）——详情页只显示「主赛」档、全部对局可见；场次标 = 头条主赛/联合主赛/主赛…（副赛/早场 tab 自然隐藏，属 App 设计的兜底行为） | 按 Match 数启发式推断（等于编数据，不做）；留空（会导致三档 tab 全隐藏、对局列表为空，**不可行**）。⚠️ 已知差异：Sherdog 源的副赛/早场档位缺失 |
| D2 | 女子量级前缀 | ✅ **按推荐**：仅 `Strawweight → Women's`（UFC 无男子草量级，可靠）；其余不加前缀，「女子蝇/雏/羽」登记为已知差异 | 建女性选手维护表（后续可从 ufc.com 交叉回填） |
| D3 | 赛事封面 `banner` | ✅ **2026-10-02 改判（原「留空」作废，用户确认）**：`ufcjson/banner.py` 用**头条双方头像**（200×300 本地图）Pillow 拼接 → `banner_local`（2 倍图 706×432，蓝左红右、**双图紧贴无缝 + VS 圆徽压中缝**、半区 cover 裁切且锚点偏上保头，与 App 封面带 353×216 同比例）；`banner` 仍为空串。头像缺失的少数赛事保持空串 → App 深色纯色底 + 渐变降级（PRD §8.1 兜底不变） | 用 200×100 `image_vs` 拉伸（糊 + 站点水印，不做）；改设计后回填 `python -m scripts.banner_maintenance --force` |
| D3′ | 封面拼图比例基准 | ⚠️ App 实现（layout/dimens）= **353×216、centerCrop**，据此出 2 倍图；PRD §7.3 / 画布（231:3）为 **353×172**（2026-10-01 加高 12 后的最新设计）—— **App 与最新设计未同步**，属既有差异、非本方案引入；App 若改为 172 需 `--force` 重拼（改 BANNER_H） | 保持现状待 App 侧对齐 |
| D4 | 迁移方式 | ✅ **按推荐**：清表重建（Sherdog 自洽库） | 混合保留 → 同赛事 ufc.com/Sherdog 双行，App 展会重复卡 |
| D5 | `player` 表范围 | ✅ **2026-09-30 收口**：`athlete`（ufc.com 版）**已整文件移除**（含其 pipeline 分支与独有的 `birth_place.py`）⇒ 不再产生跨源重复行；`player` = 事件驱动覆盖（历史赛事 + 未开打战卡 + 榜单选手，约 2,000 人级）。**名册覆盖**（没上过战卡的选手）本期不补 —— 若要补，另立「Sherdog 名册枚举」计划 | 原「等 athlete 切换后统一收口」已作废 |
| D6 | `name` / `title` 拆分 | ✅ **按推荐**：按 §3.1 规则（`UFC 331` / `Van vs. Pantoja 2`；无 `vs.` 的整串作 name） | 保留整串作 name（列表兜底展示会带对阵名） |
| D7 | 缺失头像 | ✅ **按推荐**：留空（App 用「加载失败默认图」） | 继续注入 ufc.com 占位图（跨源、且大陆不可达，不做） |

---

## 8. 实施步骤与出口判据

| 步骤 | 内容 | 出口判据 | 状态 |
|---|---|---|---|
| **S1** 重写爬虫 | `eventpass.py` 列表/详情/选手三处解析 + 映射表（§3） | 跑通第 1 页：新增 ≈100 场、无异常；抽 3 场字段对拍一致（§9） | ✅ **已完成**（2026-09-27） |
| **S2** 礼仪与健壮性 | 延迟/并发/重试进 `custom_settings`；关键节点缺失打 warning + 异常计数 | 日志含统计与结构异常计数；重试覆盖 429/403 | ✅ **已完成**（随 S1 落地） |
| **S3** 迁移 + 全量回填 | 清表 → `pagination=true` | `pass_event ≈823`、`pass_card ≈9,000`；每场 `id` 升序 = 头条主赛最前；选手页请求数 ≈ 唯一 URL 数 | ✅ **已完成**（2026-09-28，实测数据见 §8.2） |
| **S4** 收尾链核对 | 确认翻译 / 图片维护对 Sherdog 数据无害；ufc.com 时代的归一化/对账已移除（2026-09-30） | `end_method`/`card_division` 命中缓存 ≥95% | ⏸ 待 S3 后核对 |
| **S5** 契约回归 | schema + 关键查询按《历史赛事页面》§3 口径验证 | 13 表、`pass_event` 13 列、`pass_card` 15 列、`player` 35 列一致；PRD 消费字段查询在 Sherdog 库可用 | ⏸ |
| **S6** 文档与提交 | ReadMe 更新、本计划回填结论、提交 | 仓库干净、计划状态改为「已实施」 | ⏸ |

### 8.1 S1/S2 验证证据（2026-09-27，`limit` 冒烟参数 + HTML 快照解析）

新增 `-a limit=N` 冒烟参数（只取列表页前 N 场，0 = 不限）。

- **纯函数**：29/29 断言通过（名称拆分 / 地址拆段 / 时间戳 / 方式与量级映射 / 身高体重换算 / 生日 / 历史造句）；
- **列表页**（快照 `recent-events/1`）：100 条详情请求 + 1 条翻页请求；首条 `UFC Fight Night 289` / `Rosas Jr. vs. Barcelos` / `Las Vegas,Nevada,United States` / `main_time=1789776000`；
- **翻页链路**：page1→2→…；page9（23 条、无 `Older Events` 链接）正确停止；
- **详情页 UFC 331**：12 张对局卡（hero + 11 行）；`id` 序 = 卡序（第 1 张=headline Van vs Pantoja 2，最后一张=开场 Brito vs Chikadze）；hero `Flyweight Title Bout` / `Decision - Unanimous` / R5 / 5:00；表格行 `KO/TKO` / R1 / 4:56；
- **详情页 FN 289**（结果时效）：12 张；hero `Rosas Jr. vs. Barcelos`；`Disqualification` 正确归一为 `DQ`；
- **选手页**：Van 姓名/昵称/头像/`18-2-0 (W-L-D)`/`Flyweight Division`/生日 `2001-10-10`/`65.00`/`125.00`/`Myanmar`/团队 `4oz. Fight Club`/wins_stats 三数（9-2-7）/24 条全职业历史（首条=UFC 331，末条=2020 职业首秀）；
- **女子选手**（Casey O'Neill）：按 D2 不加前缀（`Flyweight Division`）；
- **契约**：`pass_event` 新 DDL 实测 13 列，与 `db-schema.md` §2.2 一致。

### 8.2 S3 全量回填实测（2026-09-27 22:59 → 2026-09-28 15:19）

| 指标 | 值 |
|---|---|
| `finish_reason` | `finished` |
| 响应数 / item 数 | 6,478 / 12,779 |
| 图片下载 | 2,745 张（webp 落 `output/images/full/`），74 张 up-to-date |
| 重试 | 23 次（超时 11 / 500 错误 7 / 下载失败 5） |
| 耗时 | 约 **16.3h**（比预估 1.5–2h 长得多——单域串行叠加 Sherdog 限流与自动限速，日常增量窗口请留足） |
| 结构异常 | 「对局缺量级」1,550（老赛事源站无数据，见 §2.4）；4 场源站无对局表 |

> ⚠️ 观测记录：该次回填完成后，本地库被再次清空（连 `player` 表一并清空、只留 Sherdog 数据），并改用**增量模式**（`-a pagination=false`，仅第 1 页 ≈100 场）重跑。若期望 App 看到完整 823 场历史，需再以 `pagination=true` 跑一次（已入场的会自动跳过，但被清空的场次需重新回填）。

---

## 9. 验收对拍清单（样例）

| 样例 | 用途 | 对拍项 |
|---|---|---|
| FN 289 Rosas Jr. vs Barcelos（2026-09-26） | 最新赛事、结果时效 | 11 场 + hero；结果/方式/回合/时间全量 |
| UFC 331 Van vs. Pantoja 2 | 冠军战标记 | hero `TITLE FIGHT` → `card_division = Flyweight Title Bout`；`cell` 与库内 ufc.com 行 diff |
| UFC 325 Volkanovski vs. Lopes 2 | 场次数量 | 12 场对局；Match 序 = `id` 序（倒序写入验证） |
| UFC 300 Pereira vs. Hill（2024） | 早期赛事/翻页边界 | page 1 尾部赛事可取；地址拆段正确 |
| Joshua Van / Casey O'Neill | 选手页字段 | 身高/体重换算（65.00 / 125.00）、`wins_stats` 三数、`record` 格式、flag emoji |

> 逐字段 diff 允许的差异：`vs.` 与 `vs` 写法、`record` 小数（125 vs 124.5）、`style`/`reach` 等已知缺口列。

---

## 10. 风险与对策

| 风险 | 对策 |
|---|---|
| Sherdog 限流（历史上被限速到 10s+/请求） | 1.5s 延迟 + 单域串行 + 重试退避；先小样试跑；全量回填分批 |
| 页面结构改版 | 关键选择器空值即 `logger.warning/error`（沿用现风格）；对拍抽查覆盖 |
| 结果未出的赛事被入库 | 7 天刷新窗口（§4.1） |
| 无分区/赔率/封面/女子前缀 | 已登记 §2.4 / §7，属**数据源固有差异**，以 App 现有降级兜底 |
| CI 调度 | DogMaker workflow 的 `on:` 目前全部注释（不会自动跑）——上线前确认触发方式与 Secrets（`LLM_*`）已配 |
| 合规 | 仅个人学习研究；控制频率（ReadMe 声明沿用） |

---

## 附录 A：取证快照（2026-09-27）

- 列表：`/recent-events/1`（100 行：2026-09-26 → 2024-08-23）、`/recent-events/2`（100 行：2024-08-17 → 2022-08-06）
- 详情：UFC 331（113633）、UFC 325（110692）、UFC 300（95109）、FN 289（113789）、UFC 332（114123，upcoming 对照）
- 选手：Joshua Van（365973）、Casey O'Neill（175007）
- 组织页统计：`9 upcoming events` / `held 823 events` / `~9,064 matches`
- 图片实测：portrait `200×300` 200 OK；旗帜 `www2-cdn …/flags/big/mm.png` 301→200；`image_vs` 200×100

## 附录 B：与端上契约的对应关系（速查）

| 契约 | 出处 | 本计划对应 |
|---|---|---|
| 三段式地址拼接 / Sherdog 前缀 | 《数据来源及图片地址》§1.1 | DogMaker 发布同构产物（§1.2） |
| 13 张表 schema | `MMABoxDocs/contract/db-schema.md` | §3 逐列映射 + §4 DDL 修正 |
| `pass_card` 排序/场次标依赖 | 《历史赛事页面》§3.3/§7.5 | §3.2 写入顺序（hero 先） |
| 冠军战徽章判定「含冠军」 | 《历史赛事页面》§7.5 | §3.2 `Title Bout` 归一 |
| 地点只拆 `address` | 《历史赛事页面》§5.4 | §3.1 地址拆段规则 |