# -*- coding: utf-8 -*-
"""历史赛事战报爬虫 —— Sherdog 数据源（DogMaker）。

数据源
------
列表：https://www.sherdog.com/organizations/Ultimate-Fighting-Championship-UFC-2/recent-events/{page}
      · 100 条/页（实测 page1 = 2026-09-26 → 2024-08-23；尾页 23 条，共 823 场）
      · ⚠️ 同一页面还有 #upcoming_tab（未来赛事、表格结构相同）——这里只认 #recent_tab
      · 翻页靠表尾 `Older Events »` 链接；该链接缺席 = 最后一页（尾页只有 Newer 链接）

详情：/events/<slug>-<id>
      · 头条主赛 = div.fight_card（Method/Round/Time 在 table.fight_card_resume 里）
      · 其余对局 = table.new_table.result 的 tr[itemprop=subEvent]，按 Match 号降序
      · Sherdog 不标主/副/早卡分区（UFC 300/325/331/332 实测无 Prelim 字样）→ card_type 全量 Main
      · 无赔率；封面不用 image_vs（实测 200×100，过小、且是头像拼图非海报）
        —— 收尾由 ufcjson/banner.py 用头条双方头像拼成封面写入 banner_local

选手：/fighter/<Name>-<id>（只取本场对战涉及的选手）

口径（与 ufc.com 版同构，App 数据契约不变）
--------------------------------------------
· pass_card 写入顺序 = 头条主赛先、其后按表格行序（Match 降序）
  ⇒ 库内 id 升序 = 卡序（App《历史赛事页面》§3.3/§7.5 的场次标与卡序依赖）
· red_page = 左侧选手（「Sherdog 左侧 → 红方」在历史审计 140/140 吻合）
· 解析与映射的统一实现在 ufcjson/sherdog.py（与 upcoming 爬虫共用）

详细取证与决策见 plans/eventpass-sherdog.md（2026-09-27）。
"""

import re
import sqlite3
from datetime import datetime, timezone

import scrapy

from ..items import UfcPassItem, UfcPassCardItem
from ..sherdog import (
    SHERDOG, abs_url, build_player_item, cell_text, cls_token, iso_to_unix,
    map_division, map_method, parse_event_name, parse_location, result_token,
    split_city_country,
)


class EventpassSpider(scrapy.Spider):
    name = "eventpass"
    # 曝光/贴画图片都在 sherdog.com 与其 CDN 子域上
    allowed_domains = [
        "sherdog.com",
        "www.sherdog.com",
        "www1-cdn.sherdog.com",
        "www2-cdn.sherdog.com",
        "www3-cdn.sherdog.com",
        "www4-cdn.sherdog.com",
    ]
    LIST_URL_TEMPLATE = SHERDOG + "/organizations/Ultimate-Fighting-Championship-UFC-2/recent-events/{page}"
    start_urls = [LIST_URL_TEMPLATE.format(page=1)]

    # 翻页上限，纯防御（实测全量 9 页）
    MAX_PAGES = 20
    # 已入库赛事的「结果刷新窗口」：窗口内的赛事即使已存在也重抓详情
    # （pass_event / pass_card 的 UPDATE 分支只补非空值，重抓安全；防「赛果未出时入库、之后永不刷新」）
    REFRESH_DAYS = 7

    custom_settings = {
        # Sherdog 会限流（历史实测被限速时单请求 10s+）：低并发 + 固定延迟 + 自动限速
        "DOWNLOAD_DELAY": 1.5,
        "CONCURRENT_REQUESTS_PER_DOMAIN": 1,
        "AUTOTHROTTLE_ENABLED": True,
        "AUTOTHROTTLE_START_DELAY": 1.5,
        "AUTOTHROTTLE_MAX_DELAY": 30,
        "AUTOTHROTTLE_TARGET_CONCURRENCY": 1.0,
        # 429/403 常是限流返回，纳入重试
        "RETRY_TIMES": 3,
        "RETRY_HTTP_CODES": [500, 502, 503, 504, 522, 524, 408, 429, 403],
    }

    def __init__(self, pagination=False, limit=0, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # pagination 为 True 时全量分页（回填用）；默认只抓第 1 页（近 2 年 100 条，足够日常增量）
        # ⚠️ 命令行 -a 传进来的永远是字符串，"false" 也是 truthy，必须显式转换
        self.pagination = str(pagination).lower() not in ("", "0", "false", "no", "none")
        # limit：仅取列表页前 N 场（冒烟测试用；0 = 不限）
        try:
            self.limit = int(limit)
        except (TypeError, ValueError):
            self.limit = 0
        # 统计信息
        self.total_new = 0
        self.total_skipped = 0
        self.total_refreshed = 0
        self.total_pages = 0
        # 结构异常计数（选择器失效的唯一信号，收尾必须打出来）
        self.anomalies = 0
        # 1. 连接到数据库（如果没有数据库文件，会自动创建）
        self.conn = sqlite3.connect('output/db/ufc.db')
        # 2. 创建游标对象（用于执行SQL语句）
        self.cursor = self.conn.cursor()

    def closed(self, reason):
        if self.anomalies:
            self.logger.warning(f"本次共发现 {self.anomalies} 处页面结构异常（见上方 warning）")
        if self.conn:
            self.conn.close()
        self.logger.info(
            f"爬取结束，共翻页 {self.total_pages} 次，新增赛事 {self.total_new} 场，"
            f"刷新 {self.total_refreshed} 场，跳过 {self.total_skipped} 场"
        )

    # ---------- 列表页 ----------

    def parse(self, response):
        """解析 Sherdog「Recent Events」列表页（只认 #recent_tab 区块）"""
        self.logger.info(f"请求列表页: {response.url}")
        current_page = self._page_no(response.url)
        rows = response.xpath(
            '//div[@id="recent_tab"]//table[{}]//tr[@itemscope]'.format(cls_token("new_table"))
        )
        self.logger.info(f"第 {current_page} 页获取到 {len(rows)} 场赛事")
        if not rows:
            self.anomalies += 1
            self.logger.error(
                "Recent 列表未取到赛事：Sherdog #recent_tab 结构可能变了，请检查选择器"
            )
            return

        new_count = skip_count = refresh_count = 0
        for row in rows:
            if self.limit and (new_count + refresh_count) >= self.limit:
                self.logger.info(f"[limit={self.limit}] 已达冒烟测试上限，停止派发详情")
                break
            href = row.xpath('.//a[@itemprop="url"]/@href').get(default="")
            if not href.strip():
                self.anomalies += 1
                self.logger.warning("列表行缺少赛事链接，跳过该行")
                continue
            url = abs_url(href)
            full_name = " ".join(
                row.xpath('.//a[@itemprop="url"]//span[@itemprop="name"]//text()').getall()
            ).strip()
            start_iso = row.xpath('.//meta[@itemprop="startDate"]/@content').get(default="")
            address = parse_location(
                row.xpath('.//td[@itemprop="location"]//text()').getall()
            )

            item = UfcPassItem()
            item["name"], item["title"] = parse_event_name(full_name)
            item["url"] = url
            item["main_time"] = iso_to_unix(start_iso)
            # Sherdog 无卡段时间：副卡/早卡留空（App 端为空不展示）
            item["prelims_time"] = ""
            item["data_early_time"] = ""
            item["address"] = address
            item["city"], item["country"] = split_city_country(address)

            exists = self._event_exists(url)
            if exists and not self._in_refresh_window(start_iso):
                self.logger.info(f"{item['name']} 已存在数据库，跳过")
                skip_count += 1
                continue
            if exists:
                refresh_count += 1
                self.logger.info(f"{item['name']} 在刷新窗口内，重抓结果")
            else:
                new_count += 1
            self.logger.info(f"准备抓取详情: {item['name']} - {url}")
            yield scrapy.Request(url=url, callback=self.parse_detail, meta={"item": item})

        self.total_new += new_count
        self.total_skipped += skip_count
        self.total_refreshed += refresh_count
        self.total_pages = current_page
        self.logger.info(
            f"第 {current_page} 页完成: 新增 {new_count} 场, 刷新 {refresh_count} 场, 跳过 {skip_count} 场"
        )

        if not self.pagination:
            return
        next_page = current_page + 1
        if next_page > self.MAX_PAGES:
            self.logger.warning(f"翻页已达上限 {self.MAX_PAGES} 页，强制停止")
            return
        older = response.xpath(
            '//div[@id="recent_tab"]//span[{}]/a[contains(text(),"Older")]/@href'.format(
                cls_token("pagination")
            )
        ).get(default="")
        if not older:
            self.logger.info("已到达最后一页（无 Older Events 链接），停止分页")
            return
        self.logger.info(f"请求第 {next_page} 页数据")
        yield scrapy.Request(url=abs_url(older), callback=self.parse)

    @staticmethod
    def _page_no(url):
        m = re.search(r"/recent-events/(\d+)", url or "")
        return int(m.group(1)) if m else 1

    def _event_exists(self, url):
        self.cursor.execute("SELECT 1 FROM pass_event WHERE page = ?", (url,))
        return self.cursor.fetchone() is not None

    def _in_refresh_window(self, start_iso):
        text = (start_iso or "").strip()
        if not text:
            return False
        try:
            dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return False
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        delta = (datetime.now(timezone.utc) - dt).total_seconds()
        return 0 <= delta <= self.REFRESH_DAYS * 86400

    # ---------- 赛事详情页 ----------

    def parse_detail(self, response):
        item = response.meta["item"]
        self.logger.info(f"抓取赛事详情: {item['name']} - {response.url}")
        # Sherdog 无赛事海报（image_vs 200×100 过小，见 plans/eventpass-sherdog.md D3）：
        # banner 原图恒为空串；封面由收尾的 ufcjson/banner.py 用头条双方头像拼接写入
        # banner_local。拼不出的少数赛事保持空串 → App 端深色纯色底 + 渐变降级
        # （《历史赛事页面》§8.1 降级矩阵）
        item["banner"] = ""

        cards = []
        hero = self._hero_card(response, item["url"])
        if hero:
            cards.append(hero)
        rows = response.xpath(
            '//table[{} and {}]//tr[@itemprop="subEvent"]'.format(
                cls_token("new_table"), cls_token("result")
            )
        )
        if not rows:
            self.anomalies += 1
            self.logger.warning(f"[detail] 未取到对局表格: {response.url}")
        for row in rows:
            cards.append(self._row_card(row, item["url"]))
        if not cards:
            self.anomalies += 1
            self.logger.warning(f"[detail] 本场没有任何对局: {response.url}")
        item["fight_cards"] = cards

        for card in cards:
            yield card
            # 空串不能直接交给 scrapy.Request（抛 ValueError: Missing scheme）
            for page in (card.get("red_page"), card.get("blue_page")):
                if page:
                    yield scrapy.Request(
                        url=page, callback=self.parse_fighter, meta={"player_page": page}
                    )
        yield item

    def _hero_card(self, response, fight_page):
        """头条主赛（div.fight_card + table.fight_card_resume）→ 第 1 张对局卡"""
        hero = response.xpath('//div[{}][1]'.format(cls_token("fight_card")))
        if not hero:
            self.anomalies += 1
            self.logger.warning(f"[detail] 未取到头条主赛区块: {response.url}")
            return None
        left = hero.xpath('./div[{} and {}][1]'.format(cls_token("fighter"), cls_token("left_side")))
        right = hero.xpath('./div[{} and {}][1]'.format(cls_token("fighter"), cls_token("right_side")))
        card = UfcPassCardItem()
        card["fight_page"] = fight_page
        card["card_type"] = "Main"
        card["red_page"] = abs_url(left.xpath('.//a[@itemprop="url"]/@href').get(default=""))
        card["blue_page"] = abs_url(right.xpath('.//a[@itemprop="url"]/@href').get(default=""))
        card["red_result"] = result_token(left)
        card["blue_result"] = result_token(right)
        weight = hero.xpath('.//span[{}]/text()'.format(cls_token("weight_class"))).get(default="")
        is_title = bool(hero.xpath('.//span[{}]'.format(cls_token("title_fight"))))
        card["card_division"] = map_division(weight, is_title)
        # resume 表列序：Match / Method / Referee / Round / Time
        tds = response.xpath('//table[{}]//td'.format(cls_token("fight_card_resume")))
        if len(tds) >= 5:
            card["end_method"] = map_method(cell_text(tds[1]))
            card["end_round"] = cell_text(tds[3])
            card["end_time"] = cell_text(tds[4])
        else:
            self.anomalies += 1
            self.logger.warning(f"[detail] 头条主赛缺 resume 表（方式/回合/时间为空）: {response.url}")
            card["end_method"] = ""
            card["end_round"] = ""
            card["end_time"] = ""
        card["red_odds"] = ""
        card["blue_odds"] = ""
        return card

    def _row_card(self, row, fight_page):
        """对局表格的一行（tr[itemprop=subEvent]）→ 对局卡"""
        card = UfcPassCardItem()
        card["fight_page"] = fight_page
        # Sherdog 无主/副/早卡分区：全量入 Main 档（计划 §7 D1），档位 tab 由 App 自然隐藏
        card["card_type"] = "Main"
        left = row.xpath('./td[2]//div[{} and {}][1]'.format(cls_token("fighter_list"), cls_token("left")))
        right = row.xpath('./td[4]//div[{} and {}][1]'.format(cls_token("fighter_list"), cls_token("right")))
        card["red_page"] = abs_url(left.xpath('.//a[@itemprop="url"]/@href').get(default=""))
        card["blue_page"] = abs_url(right.xpath('.//a[@itemprop="url"]/@href').get(default=""))
        card["red_result"] = result_token(left)
        card["blue_result"] = result_token(right)
        weight = row.xpath('./td[3]//span[{}]/text()'.format(cls_token("weight_class"))).get(default="")
        if not weight.strip():
            self.anomalies += 1
            self.logger.warning(f"[detail] 对局缺量级: {fight_page}")
        is_title = bool(row.xpath('./td[3]//span[{}]'.format(cls_token("title_fight"))))
        card["card_division"] = map_division(weight, is_title)
        card["end_method"] = map_method(row.xpath('./td[5]//b//text()').get(default=""))
        # 末两列固定是 R / Time
        card["end_round"] = " ".join(row.xpath('./td[last()-1]//text()').getall()).strip()
        card["end_time"] = " ".join(row.xpath('./td[last()]//text()').getall()).strip()
        card["red_odds"] = ""
        card["blue_odds"] = ""
        return card

    # ---------- 选手页 ----------

    def parse_fighter(self, response):
        page = response.meta.get("player_page") or response.url
        yield build_player_item(response, page, self.logger)