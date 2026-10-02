# -*- coding: utf-8 -*-
"""官方排名爬虫 —— Sherdog 自家榜单（DogMaker）。

数据源
------
索引：https://www.sherdog.com/news/rankings/list
      · 导航 `div.sub-menu.rankings` 恰好 3 条**最新榜单文章**链接（榜单每周更新、
        URL 带文章 id）⇒ 必须从导航发现，不能拼固定地址：
          DIVISIONAL RANKINGS / POUND-FOR-POUND RANKINGS / WOMEN'S POUND-FOR-POUND RANKINGS
分区榜：`/news/rankings/{page}/{slug}-{id}`（divisional 文章是多页：页 2..14 各一个量级）
      · 页内结构：`<h2>Heavyweight</h2>` 分区头 + 每名选手一条
        `<h2>1. <a href="/fighter/…">Name</a> (record) | <strong>UFC</strong> [上次名次]</h2>`
      · 每量级 **10** 人；**Sherdog 的 #1 即该量级冠军**（实测 HW #1 Gane 文案
        「promoted … to undisputed champion」、LW #1 Gaethje「defend the belt」；
        与 ufc.com 冠军快照交叉核对一致——ufc.com 那份还是上一周、冠军尚未换人）
P4P：两篇**单页**文章，同样的 `<h2>{n}. <a …>` 结构，**无冠军**（1..10）

口径（App 契约：data-files.md §4 /《选手排名》v1.7）
----------------------------------------------------
· `rank`：0 = 冠军、1..15 = 名次 ⇒ 分区榜 **Sherdog #1 → 0、#N → N-1**；
  P4P 名次原样（该档无冠军，App 端冠军带整体隐藏）
· `rank_name`：归一为 App 已知的 13 档名（男子 8 + 女子 3 + 男女 P4P）；
  ⚠️ Sherdog 另有 Women's Featherweight / Women's Atomweight —— 端上 13 档未涵盖，
  **本期不产出**（见 plans/ranking-sherdog.md §5 D2）
· `page` = Sherdog 选手 URL（绝对化）；`rank_name_cn` 不产出（端上自建映射）
· ⚠️ 与 ufc.com 版**口径不同**：这是 Sherdog 自家榜单（含 PFL/Rizin 等非 UFC 选手、
  每档 10 人而非 15 人、且更新更勤）——切换数据源即换口径，属已拍板事项
· 与 `upcoming` 同理：**顺带抓榜上选手页写 player 表**（App 排名页要中文名/头像，
  靠 `page` 与 `player.page` 精确匹配；athlete 爬虫已于 2026-09-30 停用，不补齐就整片英文名+占位头像）

产出：`output/json/ufc_ranking_data.json`（JsonObjectLinesItemExporter 流式写）。

详细取证与决策见 plans/ranking-sherdog.md（2026-09-28）。
"""

import re

import scrapy

from ..items import UfcRankingItem
from ..sherdog import SHERDOG, abs_url, build_player_item


class RankingSpider(scrapy.Spider):
    name = "ranking"
    allowed_domains = [
        "sherdog.com",
        "www.sherdog.com",
        "www1-cdn.sherdog.com",
        "www2-cdn.sherdog.com",
        "www3-cdn.sherdog.com",
        "www4-cdn.sherdog.com",
    ]
    LIST_URL = SHERDOG + "/news/rankings/list"
    start_urls = [LIST_URL]

    # 本爬虫要产出的 11 个分区（App 13 档的另外 2 档是 P4P，来自两篇独立文章）
    # ⚠️ 顺序 = 写入顺序 = 端上 rail 顺序（PRD：P4P 男→女→11 量级，这里只需保证量级段有序）
    DIVISIONS = (
        "Heavyweight",
        "Light Heavyweight",
        "Middleweight",
        "Welterweight",
        "Lightweight",
        "Featherweight",
        "Bantamweight",
        "Flyweight",
        "Women's Strawweight",
        "Women's Flyweight",
        "Women's Bantamweight",
    )

    custom_settings = {
        # 与 eventpass / upcoming 同口径的 Sherdog 礼仪
        "DOWNLOAD_DELAY": 1.5,
        "CONCURRENT_REQUESTS_PER_DOMAIN": 1,
        "AUTOTHROTTLE_ENABLED": True,
        "AUTOTHROTTLE_START_DELAY": 1.5,
        "AUTOTHROTTLE_MAX_DELAY": 30,
        "AUTOTHROTTLE_TARGET_CONCURRENCY": 1.0,
        "RETRY_TIMES": 3,
        "RETRY_HTTP_CODES": [500, 502, 503, 504, 522, 524, 408, 429, 403],
    }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # 结构异常计数（选择器失效的唯一信号，收尾必须打出来）
        self.anomalies = 0
        self.total_ranked = 0

    def closed(self, reason):
        if self.anomalies:
            self.logger.warning(f"本次共发现 {self.anomalies} 处页面结构异常（见上方 warning）")
        self.logger.info(f"排名抓取结束，共产出 {self.total_ranked} 条排名")

    # ---------- 索引页：发现最新三篇榜单文章 ----------

    def parse(self, response):
        links = self._latest_articles(response)
        if len(links) != 3:
            self.anomalies += 1
            self.logger.error(
                f"榜单导航未取全三篇最新文章（拿到 {sorted(links)}）："
                "Sherdog 的 sub-menu.rankings 结构可能变了，请检查选择器"
            )
            return
        # ⚠️ 写入顺序 = 端上 rail 顺序（《选手排名》v1.3：P4P 男 → 女 → 11 量级）。
        # Scrapy 同优先级是 **LIFO**，所以这里用递减 priority 显式定序：
        # 男 P4P(100) → 女 P4P(90) → 各量级(50 起递减，见 parse_divisional)。
        yield scrapy.Request(
            links["p4p"], callback=self.parse_p4p, priority=100,
            meta={"rank_name": "Men's Pound-for-Pound"},
        )
        yield scrapy.Request(
            links["women_p4p"], callback=self.parse_p4p, priority=90,
            meta={"rank_name": "Women's Pound-for-Pound"},
        )
        # 分区榜首页（多页文章的 page 1）→ 从 <select> 拿各量级页
        yield scrapy.Request(links["divisional"], callback=self.parse_divisional)

    def _latest_articles(self, response):
        """`div.sub-menu.rankings` 的三条链接（DIVISIONAL / P4P / WOMEN'S P4P）"""
        out = {}
        for a in response.xpath(
            '//div[contains(@class,"sub-menu") and contains(@class,"rankings")]/a'
        ):
            label = " ".join(a.xpath(".//text()").getall()).strip().upper()
            href = abs_url(a.xpath("./@href").get(default=""))
            if not href:
                continue
            if "WOMEN" in label and "POUND" in label:
                out["women_p4p"] = href
            elif "POUND" in label:
                out["p4p"] = href
            elif "DIVISIONAL" in label:
                out["divisional"] = href
        return out

    # ---------- 分区榜（多页文章） ----------

    def parse_divisional(self, response):
        """divisional 文章 page 1：`<select>` 的 option = 各量级页（value 就是 URL）

        ⚠️ 派发顺序 = 写入顺序 = 端上 rail 顺序：按本文件 `DIVISIONS` 的次序排序后再派发，
        并给递减 priority（Scrapy 同优先级 LIFO，不显式定序会得到反序 rail）。
        """
        options = response.xpath("//select//option")
        if not options:
            self.anomalies += 1
            self.logger.error(f"分区榜文章未取到分页下拉: {response.url}")
            return
        targets = []
        for opt in options:
            # label 形如 `Page 6 - Lightweight`；page 1 是引言（无 `" - "`），自然被跳过
            label = " ".join(opt.xpath(".//text()").getall()).strip()
            href = abs_url(opt.xpath("./@value").get(default=""))
            division = self._norm_division(label.split(" - ", 1)[1]) if " - " in label else ""
            if not href or not division:
                continue
            if division not in self.DIVISIONS:
                self.logger.info(f"跳过未纳入端上 13 档的分区: {division}")
                continue
            targets.append((self.DIVISIONS.index(division), href, division))
        for order, (index, href, division) in enumerate(sorted(targets)):
            yield scrapy.Request(
                href, callback=self.parse_division, priority=50 - order,
                meta={"division": division},
            )

    def parse_division(self, response):
        """量级页：每名选手一条，Sherdog #1 = 冠军 → 库内 rank 0"""
        division = response.meta["division"]
        entries = self._entries(response)
        if not entries:
            self.anomalies += 1
            self.logger.warning(f"[ranking] {division} 未取到排名条目: {response.url}")
            return
        for sherdog_rank, name, href in entries:
            item = UfcRankingItem()
            item["rank_name"] = division
            item["name"] = name
            item["page"] = href
            # Sherdog #1 = 该量级冠军 ⇒ 库内 0；#N → N-1
            item["rank"] = max(sherdog_rank - 1, 0)
            self.total_ranked += 1
            yield item
            yield scrapy.Request(
                href, callback=self.parse_fighter, meta={"player_page": href}
            )

    # ---------- P4P（两篇单页文章，无冠军） ----------

    def parse_p4p(self, response):
        rank_name = response.meta["rank_name"]
        entries = self._entries(response)
        if not entries:
            self.anomalies += 1
            self.logger.warning(f"[ranking] {rank_name} 未取到排名条目: {response.url}")
            return
        for rank, name, href in entries:
            item = UfcRankingItem()
            item["rank_name"] = rank_name
            item["name"] = name
            item["page"] = href
            # P4P 无冠军：名次原样（1..N），App 端该档冠军带整体隐藏
            item["rank"] = rank
            self.total_ranked += 1
            yield item
            yield scrapy.Request(
                href, callback=self.parse_fighter, meta={"player_page": href}
            )

    # ---------- 共用 ----------

    def _entries(self, response):
        """文章正文里的排名条目 → [(名次, 姓名, 选手页 URL), ...]

        条目标记：`<h2>1. <a href="/fighter/…">Name</a> (record) | <strong>UFC</strong> [上次名次]</h2>`
        ⚠️ 男子 P4P 的 #1 在站点上是 `1. 1. <a …>`（重复了一次名次），取第一个数字即可。
        """
        out = []
        for h2 in response.xpath(
            '//div[contains(@class,"body_content")]//h2[a[contains(@href,"/fighter/")]]'
        ):
            link = h2.xpath('.//a[contains(@href,"/fighter/")][1]')
            href = abs_url(link.xpath("./@href").get(default=""))
            name = re.sub(r"\s+", " ", " ".join(link.xpath(".//text()").getall())).strip()
            head = " ".join(h2.xpath("./text()").getall())
            m = re.search(r"(\d+)\s*\.", head)
            if not href or not name or not m:
                self.anomalies += 1
                self.logger.warning(f"[ranking] 条目解析失败（缺名次/姓名/链接）: {response.url}")
                continue
            out.append((int(m.group(1)), name, href))
        return out

    def _norm_division(self, text):
        """`Women’s Strawweight`（排版撇号）→ `Women's Strawweight`（库内/端上口径）"""
        text = (text or "").replace("’", "'").replace("‘", "'").replace("`", "'")
        return re.sub(r"\s+", " ", text).strip()

    def parse_fighter(self, response):
        page = response.meta.get("player_page") or response.url
        yield build_player_item(response, page, self.logger)