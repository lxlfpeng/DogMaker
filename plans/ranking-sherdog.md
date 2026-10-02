# ranking 切换 Sherdog 数据源（自家榜单）· 实施计划

> 文档：`DogMaker/plans/ranking-sherdog.md`　版本：v0.1（2026-09-28）
> 范围：`DogMaker/ufcjson/spiders/ranking.py`（官方排名 → `ufc_ranking_data.json`）
> 数据源：**Sherdog 自家榜单**（2026-09-28 用户拍板「ranking 用自家榜单」）
> 上游依据：`Resources/contract/data-files.md` §4、《选手排名》v1.7（rail 13 档 / rank 语义 / 冠军带）

---

## 0. 目标与非目标

**目标**：`ranking` 从 ufc.com 官方排名切到 Sherdog 自家榜单，产出与契约同构的 `ufc_ranking_data.json`（字段与 rank 语义不变）。
**非目标**：`athlete` 仍未切换（另立计划）；端上不改（端上 13 档中文映射尚未实现，本爬虫**保持 ufc.com 的命名口径**以便端上按同一套名字建表）。

---

## 1. 为什么必须先拍板数据源

- ufc.com 的 `/rankings` 是 **UFC 官方排名**（冠军 + #1–15，仅 UFC 选手）；
- Sherdog **没有** UFC 官方排名，只有它自家的 "Sherdog's official MMA rankings"（`/news/rankings/…`）：
  **跨组织**（含 PFL / Rizin / MVP 选手）、**每档 10 人**、**#1 即冠军**、更新更勤（每周）；
- 两者口径不同，切换数据源＝换榜单口径 —— 用户 2026-09-28 拍板采用**自家榜单**，本文按此实施。

---

## 2. 已实测页面结构（取证 2026-09-28，选择器可直接落地）

### 2.1 索引页 `/news/rankings/list`

导航 `div.sub-menu.rankings` 恰好 3 条链接，指向**当前最新**的三篇榜单文章（URL 带文章 id，必须从这里发现、不能拼固定地址）：

| 链接文案 | 用途 |
|---|---|
| `DIVISIONAL RANKINGS` | 分区榜（多页文章） |
| `POUND-FOR-POUND RANKINGS` | 男子 P4P（单页） |
| `WOMEN'S POUND-FOR-POUND RANKINGS` | 女子 P4P（单页） |

### 2.2 分区榜文章（多页）

- page 1 = 引言（无榜单）；页面 `<select>` 的 `<option value="…">{Page N - Division}</option>` = 各量级页；
- 实测 page 2..14 = 13 个分区：HW / LHW / MW / WW / LW / FW / BW / FLW / **Women's FW** / WBW / **Women's FLW** / WSW / **Women's Atomweight**；
- 量级页结构：`<h2>Heavyweight</h2>` 分区头 + 每名选手一条
  `<h2>1. <a href="/fighter/Ciryl-Gane-293973">Ciryl Gane</a> (14-2, 1 NC) | UFC [1]</h2>` + 一段点评；
- **每档 10 人**；`[n]` 是上次名次（`[NR]` = 新进），本期不用。

### 2.3 P4P 文章（单页）

结构同分区页条目（`<h2>{n}. <a href="/fighter/…">Name</a>…`），**无冠军**，1..10；
⚠️ 男子 P4P 的 #1 在站点上是 `1. 1. <a …>`（名次重复一次）——取第一个数字即可。

### 2.4 冠军判定（本方案的关键假设）

**Sherdog 的 #1 即该量级冠军**，证据：

- HW #1 Gane 的点评原文：`With Aspinall vacating the title, Gane has been promoted from interim heavyweight titleholder to undisputed champion`；
- LW #1 Gaethje 的点评承接句：`Assuming he decides to defend the belt … his first title defense`；
- 与库内 ufc.com 冠军快照交叉核对一致（LW #1 = Gaethje ✓、Women's Strawweight #1 = Dern ✓；HW 不一致是因为 ufc.com 快照还是上一周、冠军尚未换人）。

---

## 3. 字段映射（Sherdog → `ufc_ranking_data.json`）

| JSON 字段 | 来源 | 规则 |
|---|---|---|
| `rank_name` | 分区名（select label / 页面 h2 归一）或固定 P4P 名 | 归一为**端上已知的 13 档名**（8 男子 + 3 女子 + `Men's/Women's Pound-for-Pound`）；排版撇号 `Women’s` → `Women's` |
| `name` | 条目锚文本 | 多行拼回（`Islam\nMakhachev` → `Islam Makhachev`） |
| `page` | 条目 `a/@href` | 绝对化（Sherdog 选手 URL） |
| `rank` | 条目名次 | **分区榜：Sherdog #1 → `0`（冠军）、#N → `N-1`**；P4P：原样 `1..10`（无冠军） |

### 3.1 写入顺序 = 端上 rail 顺序 [MUST]

《选手排名》v1.3：rail 档位按**数据出现顺序**渲染（P4P 男 → 女 → 11 量级）。
⚠️ Scrapy 同优先级是 **LIFO**，因此本爬虫用**递减 priority 显式定序**：男 P4P(100) → 女 P4P(90) → 量级按 `DIVISIONS` 次序(50 递减)。

---

## 4. 代码改动清单

| 文件 | 改动 |
|---|---|
| `ufcjson/spiders/ranking.py` | **整文件重写**（Sherdog）：索引导航发现 3 篇 → 分区榜 select 派发 11 个量级页 → 逐条产出 `UfcRankingItem` |
| `ufcjson/pipelines/export_json.py` | **修 bug**：原 `if isinstance(spider, RankingSpider): export_item(item)` 会导出**该爬虫的所有 item** —— 新爬虫随行产出 `UfcPlayerItem` 后，选手对象会整批混进排名 JSON（实弹首跑得 243 条 = 130 排名 + 113 选手）。改为 `and isinstance(item, UfcRankingItem)` |
| `ufcjson/sherdog.py` | 复用（选手页 `build_player_item`）——本爬虫也会抓榜上选手页写 `player` |
| `ReadMe.md` | 数据来源 / 命令 / 结构行 |
| `plans/ranking-sherdog.md` | 本文件 |

### 4.1 与 ufc.com 版的有意差异

1. **榜单口径**：Sherdog 自家（跨组织、10 人/档、更新更勤）——已拍板；
2. **顺带抓榜上选手页写 `player` 表**：App 排名页要中文名/头像，靠 `page` 与 `player.page` 精确匹配；`athlete` 已于 **2026-09-30 移除**，不补齐就整片英文名 + 占位头像。成本 ~130 请求/轮；
3. 每档 10 人（原 16 行含冠军）；`rank` 上限 9（原 15）——端上按数据渲染，不做补齐。

---

## 5. 决策项

| # | 事项 | 结论 | 说明 |
|---|---|---|---|
| D1 | 榜单数据源 | ✅ **Sherdog 自家榜单**（用户拍板） | 跨组织内容一并在内（如 HW #3 Ngannou / PFL） |
| D2 | Sherdog 多出的两个分区（Women's Featherweight / Women's Atomweight） | ✅ **本期不产出** | 端上 13 档未涵盖；产出未知档名会给端上 rail 带来未定义档位。如端上要扩档，再补 |
| D3 | `#1` = 冠军（→ `rank 0`） | ✅ 采纳（证据见 §2.4） | ⚠️ 已知风险：**空缺/停摆量级**的 #1 并非冠军时会被端上误显示为冠军；本方案接受（Sherdog 该档本身也是「无冕之王」语义） |
| D4 | 顺带抓选手页 | ✅ 采纳 | 见 §4.1-2 |
| D5 | 写入顺序显式定序 | ✅ 采纳 | 见 §3.1（Scrapy LIFO 坑） |

---

## 6. 实施步骤与出口判据

| 步骤 | 内容 | 出口判据 | 状态 |
|---|---|---|---|
| **S1** 重写 `ranking.py` | 索引 + 分区榜 + P4P + 选区过滤 | 快照验证全绿（见 §7） | ✅ 2026-09-28 |
| **S2** 实弹跑 | `scrapy crawl ranking` | 13 档齐、每档 10 条、rail 顺序正确、0 结构异常 | ✅ 2026-09-28（数据见 §7 末行） |
| **S3** 文档与提交 | ReadMe + 本文件 | 仓库干净 | ⏸ |

---

## 7. 验证证据（2026-09-28，HTML 快照 + 实弹）

- **索引页**：恰取到 3 篇最新文章（DIVISIONAL / P4P / WOMEN'S P4P）；
- **分区榜首页**：`<select>` 解析出 13 个分区，**派发 11 个**（跳过 Women's FW / Atomweight 并留日志）；顺序 = `DIVISIONS` 定义序；
- **量级页**（HW / LW / WSW 抽样）：每档 **10 条**、Sherdog #1 → `rank 0`（HW 首条 = `Ciryl Gane` ✓ 冠军）、#10 → `rank 9`；每页各派 10 个选手页请求；
- **P4P**：男/女各 10 条，名次原样 1..10（男 #1 Islam Makhachev、女 #1 Valentina Shevchenko），**无 rank 0** ✓；
- **实弹**：`scrapy crawl ranking` 跑完（`finished`）——`ufc_ranking_data.json` 已由 Sherdog 榜单覆盖：13 档 × 10 条 = **130 条**，rail 顺序 = 男 P4P → 女 P4P → HW…WBW，榜上选手行写入 `player` 表（本轮新增 113 名选手页）；
- **首跑暴露并修复一个管道 bug**：排名 JSON 里混入了 113 条选手对象（`export_json.py` 对 RankingSpider 不区分 item 类型）→ 已限定 `UfcRankingItem` 并重跑复核（见 §4 与 §8）；
- **修复后复核（2026-09-29 00:15 跑完）**：`ufc_ranking_data.json` = **130 条、0 混入**；档位顺序 = 男 P4P → 女 P4P → HW/LHW/MW/WW/LW/FW/BW/FLW → WSW/WFLW/WBW；**11 个量级有 `rank 0` 冠军**（HW Ciryl Gane、LHW Alex Pereira、MW Sean Strickland、WW Islam Makhachev、LW Justin Gaethje、FW Alexander Volkanovski、BW Petr Yan、FLW Joshua Van、WSW Mackenzie Dern、WFLW Valentina Shevchenko、WBW Kayla Harrison），两个 P4P 无 `rank 0` ✓。

---

## 8. 风险与对策

| 风险 | 对策 |
|---|---|
| 榜单文章 URL 每周变 | 从 `/news/rankings/list` 导航的 3 条链接发现（不拼地址）；取不全即 error 留痕 |
| 空缺量级的 #1 被当成冠军（D3） | 已登记；若产品不接受，需另一数据源判定冠军（如人工维护表） |
| 跨组织选手无 `player` 行 | 本爬虫顺带抓其选手页（D4）；仍缺的按 App §8.1 降级（英文名 + 加载失败默认图） |
| 每档 10 人 vs 端上 15 人预期 | 端上按数据渲染；PRD「缺档隐藏」语义只针对档位、不针对人数 |
| Sherdog 限流 | 与 eventpass / upcoming 同口径礼仪 |