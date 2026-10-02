# -*- coding: utf-8 -*-
"""即将到来的赛事爬虫 —— Sherdog 数据源（DogMaker）。

数据源
------
列表：https://www.sherdog.com/organizations/Ultimate-Fighting-Championship-UFC-2/recent-events/1
      · ⚠️ 未开打赛事与历史赛事在**同一张组织页**上，靠 tab 区分：
        这里只认 #upcoming_tab（实测 9 条：2026-10-03 ~ 2026-12-12）
      · 字段只有「日期 / 赛事名 / 地点」三项，且日期只有**到日**（当天 00:00 UTC，
        不是真实开赛时刻）——`main_time` 只能给到当天，App 端倒计时/「到分钟」的
        日期会偏早，属已登记的源站缺口（见 plans/upcoming-sherdog.md §2.4）

详情：/events/<slug>-<id>（未开打版本）
      · 头条主赛 = div.fight_card（选手角标是 `final_result yet_to_come`，无结果）
      · 其余对局 = table.new_table.upcoming 的 tr[itemprop=subEvent]
        （列：Match / 左选手 / 量级 / 右选手 / 空；**无方式、回合、时间、赔率**）
      · 没有任何主/副/早卡分区标记 → card_type 全量 Main（与 eventpass 同一决策）
      · 赛事图仅 image_vs（200×100，过小）→ banner / banner_local 留空（App 深色底降级）

选手：/fighter/<Name>-<id>
      · ⚠️ 与 ufc.com 版**有意不同**：本爬虫也会抓战卡双方选手页并写 player 表。
        原因：Sherdog 的 coming JSON 里 red/blue 是 Sherdog 选手 URL，而 athlete 爬虫
        已于 2026-09-30 停用 ⇒ 不自己补齐的话，App 端赛程页/详情页会「查不到选手」
        而整片显示 TBD + 占位头像。

产出
----
只出 `output/json/ufc_coming_data.json`（走 JsonWriterPipeline，**不写数据库表**；
player 行由导出管道写）。字段契约见 MMABoxDocs/contract/data-files.md §3；
`name` 归一为 App 内置展示映射的标识（`UFCFightNight` / `UFC<数字>`，见 sherdog.py）。

详细取证与决策见 plans/upcoming-sherdog.md（2026-09-28）。
"""

import scrapy

from ..items import UfcComingItem, UfcComingCardItem
from ..sherdog import (
    SHERDOG, abs_url, build_player_item, cls_token, iso_to_unix,
    map_coming_name, map_division, normalize_vs, parse_event_name, parse_location,
)


class UpcomingSpider(scrapy.Spider):
    name = "upcoming"
    allowed_domains = [
        "sherdog.com",
        "www.sherdog.com",
        "www1-cdn.sherdog.com",
        "www2-cdn.sherdog.com",
        "www3-cdn.sherdog.com",
        "www4-cdn.sherdog.com",
    ]
    # 未开打赛事与历史赛事同页，只有 tab 不同（#upcoming_tab 在第 1 页即可全见）
    LIST_URL = SHERDOG + "/organizations/Ultimate-Fighting-Championship-UFC-2/recent-events/1"
    start_urls = [LIST_URL]

    custom_settings = {
        # 与 eventpass 同口径的 Sherdog 礼仪（低并发 + 固定延迟 + 限流重试）
        "DOWNLOAD_DELAY": 1.5,
        "CONCURRENT_REQUESTS_PER_DOMAIN": 1,
        "AUTOTHROTTLE_ENABLED": True,
        "AUTOTHROTTLE_START_DELAY": 1.5,
        "AUTOTHROTTLE_MAX_DELAY": 30,
        "AUTOTHROTTLE_TARGET_CONCURRENCY": 1.0,
        "RETRY_TIMES": 3,
        "RETRY_HTTP_CODES": [500, 502, 503, 504, 522, 524, 408, 429, 403],
    }

    def __init__(self, limit=0, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # limit：只取列表页前 N 场（冒烟测试用；0 = 不限）
        try:
            self.limit = int(limit)
        except (TypeError, ValueError):
            self.limit = 0
        # 结构异常计数（选择器失效的唯一信号，收尾必须打出来）
        self.anomalies = 0

    def closed(self, reason):
        if self.anomalies:
            self.logger.warning(f"本次共发现 {self.anomalies} 处页面结构异常（见上方 warning）")

    # ---------- 列表页 ----------

    def parse(self, response):
        """解析 Sherdog 组织页的 #upcoming_tab（未开打赛事）"""
        self.logger.info(f"请求列表页: {response.url}")
        rows = response.xpath(
            '//div[@id="upcoming_tab"]//table[{}]//tr[@itemscope]'.format(cls_token("new_table"))
        )
        self.logger.info(f"获取到 {len(rows)} 场即将到来的赛事")
        if not rows:
            self.anomalies += 1
            self.logger.error(
                "Upcoming 列表未取到赛事：Sherdog #upcoming_tab 结构可能变了，请检查选择器"
            )
            return

        for row in rows[: self.limit or None]:
            href = row.xpath('.//a[@itemprop="url"]/@href').get(default="")
            if not href.strip():
                self.anomalies += 1
                self.logger.warning("列表行缺少赛事链接，跳过该行")
                continue
            url = abs_url(href)
            full_name = " ".join(
                row.xpath('.//a[@itemprop="url"]//span[@itemprop="name"]//text()').getall()
            ).strip()
            name, title = parse_event_name(full_name)
            start_iso = row.xpath('.//meta[@itemprop="startDate"]/@content').get(default="")

            item = UfcComingItem()
            # name 归一为 App 内置映射能识别的标识（UFCFightNight / UFC<数字>），
            # title 做 vs. → vs 归一、TBA/TBD → `TBD vs TBD`（对齐 ufc.com 口径）
            item["name"] = map_coming_name(name)
            item["title"] = normalize_vs(title)
            item["page"] = url
            item["main_time"] = iso_to_unix(start_iso)
            # Sherdog 无卡段时间：副赛/早卡留空（App 端为空不展示）
            item["prelims_time"] = ""
            item["data_early_time"] = ""
            item["address"] = parse_location(
                row.xpath('.//td[@itemprop="location"]//text()').getall()
            )
            self.logger.info(f"准备抓取详情: {item['name']} - {item['title']} - {url}")
            yield scrapy.Request(url=url, callback=self.parse_detail, meta={"item": item})

    # ---------- 赛事详情页（未开打） ----------

    def parse_detail(self, response):
        item = response.meta["item"]
        self.logger.info(f"抓取赛事详情: {item['name']} - {response.url}")
        # Sherdog 赛事图只有 image_vs（200×100，过小），不作横幅：App 端 banner_local 空
        # 时头条大图走深色纯色底 + 渐变（《赛程页面》§7.3 / §8.3 降级）
        item["banner"] = ""

        fight_cards = []
        item["fight_card"] = fight_cards
        hero = self._hero_card(response, item)
        if hero:
            fight_cards.append(hero)
        rows = response.xpath(
            '//table[{}]//tr[@itemprop="subEvent"]'.format(cls_token("upcoming"))
        )
        if not rows and not hero:
            # 完全未定阵的赛事（如 `UFC 335 - TBA` 的早期形态）：App 端走「对阵待定」态
            self.anomalies += 1
            self.logger.warning(f"[detail] 未取到任何战卡（对阵待定）: {response.url}")
        for row in rows:
            fight_cards.append(self._row_card(row, item))

        for card in fight_cards:
            # 卡只随 item['fight_card'] 出 JSON；这里只派发选手页请求
            for page in (card.get("red_page"), card.get("blue_page")):
                if page:
                    yield scrapy.Request(
                        url=page, callback=self.parse_fighter, meta={"player_page": page}
                    )
        self.logger.info(f"完成抓取比赛: {item['name']}，共 {len(fight_cards)} 张对局卡")
        yield item

    def _hero_card(self, response, event):
        """头条主赛（div.fight_card）→ 第 1 张对局卡（未开打时角标是 `yet to come`）"""
        hero = response.xpath('//div[{}][1]'.format(cls_token("fight_card")))
        if not hero:
            self.anomalies += 1
            self.logger.warning(f"[detail] 未取到头条主赛区块: {response.url}")
            return None
        left = hero.xpath('./div[{} and {}][1]'.format(cls_token("fighter"), cls_token("left_side")))
        right = hero.xpath('./div[{} and {}][1]'.format(cls_token("fighter"), cls_token("right_side")))
        card = self._base_card(event)
        card["red_page"] = abs_url(left.xpath('.//a[@itemprop="url"]/@href').get(default=""))
        card["blue_page"] = abs_url(right.xpath('.//a[@itemprop="url"]/@href').get(default=""))
        weight = hero.xpath('.//span[{}]/text()'.format(cls_token("weight_class"))).get(default="")
        is_title = bool(hero.xpath('.//span[{}]'.format(cls_token("title_fight"))))
        card["card_division"] = map_division(weight, is_title)
        return card

    def _row_card(self, row, event):
        """对局表格的一行（tr[itemprop=subEvent]）→ 对局卡（列：Match/左/量级/右/空）"""
        card = self._base_card(event)
        left = row.xpath('./td[2]//div[{} and {}][1]'.format(cls_token("fighter_list"), cls_token("left")))
        right = row.xpath('./td[4]//div[{} and {}][1]'.format(cls_token("fighter_list"), cls_token("right")))
        card["red_page"] = abs_url(left.xpath('.//a[@itemprop="url"]/@href').get(default=""))
        card["blue_page"] = abs_url(right.xpath('.//a[@itemprop="url"]/@href').get(default=""))
        weight = row.xpath('./td[3]//span[{}]/text()'.format(cls_token("weight_class"))).get(default="")
        if not weight.strip():
            self.anomalies += 1
            self.logger.warning(f"[detail] 对局缺量级: {event['page']}")
        is_title = bool(row.xpath('./td[3]//span[{}]'.format(cls_token("title_fight"))))
        card["card_division"] = map_division(weight, is_title)
        return card

    @staticmethod
    def _base_card(event):
        """赛事级字段的公共部分（契约：卡上也要带 main_time / address / fight_name）。"""
        card = UfcComingCardItem()
        # Sherdog 无主/副/早卡分区：全量入 Main 档（计划 D1），档位 tab 由 App 自然隐藏
        card["card_type"] = "Main"
        card["fight_name"] = event["name"]
        card["main_time"] = event["main_time"]
        card["address"] = event["address"]
        # 源站缺口：无赔率、无排名（App 端各自降级/隐藏）、无对阵 ID
        card["red_odds"] = ""
        card["blue_odds"] = ""
        card["red_rank"] = ""
        card["blue_rank"] = ""
        card["fight_id"] = ""
        return card

    # ---------- 选手页 ----------

    def parse_fighter(self, response):
        page = response.meta.get("player_page") or response.url
        yield build_player_item(response, page, self.logger)