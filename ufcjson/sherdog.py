# -*- coding: utf-8 -*-
"""Sherdog 页面解析共用层（eventpass / upcoming 两个爬虫的唯一实现）。

为什么单独成模块
----------------
`eventpass`（历史赛事）与 `upcoming`（未开打赛事）解析的是**同一套 Sherdog 页面**：
列表在同一张组织页（`#recent_tab` / `#upcoming_tab` 两个 tab）、详情页共用同一套
`div.fight_card` + `table.new_table.*` 结构、选手页完全同一页。把纯函数与选手页解析
放这里，两个爬虫各自只保留「读哪张表 / 产出哪个 item」的差异，避免两套实现漂移。

口径（与 App 契约对齐，详见 plans/eventpass-sherdog.md、plans/upcoming-sherdog.md）
----------------------------------------------------------------------------------
· `parse_location` 去场馆段 → 库内 `City,State,Country` 形态（App 按「首段=城市、末段=国家」消费）
· `map_division` / `map_method` 归一为库内词表 → 命中现有翻译缓存（零新增 LLM 调用）
· `build_player_item` 的 `page` 一律用**请求 URL**（页面给的 href），不取 `response.url`，
  保证与 `pass_card` / coming JSON 的 red/blue 引用逐字一致（App 端精确等值关联）
· 老赛事量级：Sherdog 的 `span.weight_class` 存在但可能为空 → 留空不猜
"""

import re
from datetime import datetime, timezone
from urllib.parse import urljoin

from .items import UfcPlayerItem

SHERDOG = "https://www.sherdog.com"

# 结果角标 class 词元 → 库内口径（App 端只认这 4 种）
RESULT_MAP = {
    "win": "Win",
    "loss": "Loss",
    "draw": "Draw",
    "nc": "NC",
    "no_contest": "NC",
}

# 胜场 meter 标题 → 库内 wins_stats 词表（三条译文缓存已齐）
WINS_STATS_WAY = {
    "ko/tko": "Wins by Knockout",
    "submissions": "Wins by Submission",
    "decisions": "Wins by Decision",
}

# 3 段地点行「首段是否场馆」的判据（≥4 段时无条件丢首段，不依赖本表）
VENUE_KEYWORDS = (
    "Arena", "Apex", "Center", "Centre", "Stadium", "Garden", "Hall", "Dome",
    "Coliseum", "Casino", "Theatre", "Theater", "Live", "O2", "Park", "Field",
    "Auditorium", "Convention", "Ballroom", "Gymnasium",
)

# 地点国家段别名归一（与库内 ufc.com 口径一致，如 London,United Kingdom）
COUNTRY_ALIASES = {
    "England": "United Kingdom",
    "Scotland": "United Kingdom",
    "Wales": "United Kingdom",
    "Macau": "Macao SAR China",
}

MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}


def cls_token(name):
    """XPath 片段：按 class 词元精确匹配（防 `wins` 误命中 `winsloses`）。"""
    return 'contains(concat(" ", normalize-space(@class), " "), " {} ")'.format(name)


def abs_url(href):
    """相对路径 → 绝对 URL；空值 / 非页面链接（`javascript:void();` 等）返回空串。

    ⚠️ Sherdog 的「双方未定」对局会挂 `javascript:void();` 当 href（实测 UFC Qatar 页）。
    这类值绝不能进 `scrapy.Request`（抛 ValueError: Missing scheme 并让整场 item 丢失），
    也不能落库当选手页；统一在这里挡成空串，由 App 端走 TBD 降级。
    """
    href = (href or "").strip()
    if not href:
        return ""
    url = href if href.lower().startswith(("http://", "https://")) else urljoin(SHERDOG + "/", href)
    if not url.lower().startswith(("http://", "https://")):
        return ""
    return url


def cell_text(td):
    """td 的直接文本节点拼成值（`<em>Method</em><br/> Decision (Unanimous)` → `Decision (Unanimous)`）"""
    return " ".join(t.strip() for t in td.xpath("text()").getall() if t.strip()).strip()


def result_token(scope):
    """final_result 角标 → 库内结果口径；取不到返回空串。"""
    cls = scope.xpath('.//span[{}]/@class'.format(cls_token("final_result"))).get(default="")
    token = cls.replace("final_result", "").strip().lower()
    return RESULT_MAP.get(token, "")


def parse_event_name(full_name):
    """`UFC 331 - Van vs. Pantoja 2` → (`UFC 331`, `Van vs. Pantoja 2`)。

    按第一个 `" - "` 拆分，但**头部是光秃秃的 `UFC` 时不拆**——那类名字形如
    `UFC - Road to UFC Season 5: Shanghai Semifinals`，拆出来只剩 `UFC` 没有信息量，
    整串作 name 更合适。尾段既可能是对阵（`Van vs. Pantoja 2`），也可能是
    赛事副标题（`Noche UFC 4`）或 `TBA`，与库内 ufc.com 的 `title` 写法一致。
    """
    text = re.sub(r"\s+", " ", (full_name or "")).strip()
    if not text:
        return "", ""
    head, sep, tail = text.partition(" - ")
    if sep and tail.strip() and head.strip().lower() != "ufc":
        return head.strip(), tail.strip()
    return text, ""


def map_coming_name(event_name):
    """赛事名 → 与 App 内置展示映射对齐的标识（《赛程页面》§5.6）。

    App 的映射表只有 `UFCFightNight`（→「UFC 格斗之夜」）+ 正则 `^UFC(\\d+)$`（→「UFC N」），
    其余按原文展示。故：
      · `UFC 332` → `UFC332`（命中 `^UFC(\\d+)$`）
      · `UFC Fight Night 290` → `UFCFightNight`
      · 其余（`UFC Qatar` / `UFC on ESPN 73` / Road to UFC 等）→ 原文，不猜
    """
    text = re.sub(r"\s+", " ", (event_name or "")).strip()
    if not text:
        return ""
    m = re.fullmatch(r"UFC\s*(\d+)", text, re.I)
    if m:
        return "UFC{}".format(m.group(1))
    if "fight night" in text.lower():
        return "UFCFightNight"
    return text


def normalize_vs(text):
    """`Silva vs. Wang` → `Silva vs Wang`；`TBA`/`TBD` 单值 → `TBD vs TBD`（对齐 ufc.com 口径）。"""
    text = re.sub(r"\s+", " ", (text or "")).strip()
    if not text:
        return ""
    if text.upper() in ("TBA", "TBD"):
        return "TBD vs TBD"
    return re.sub(r"\bvs\.\s*", "vs ", text)


def parse_location(text_nodes):
    """Sherdog 地点行 → 库内形态（`City,State,Country` / `City,Country`）。

    规则：
      ① ≥4 段（Venue, City, Region, Country）→ 丢首段
      ② 3 段（Venue, City, Country）→ 首段命中场馆关键词才丢
      ③ ≤2 段 → 原样
      ④ 国家段别名归一（England/Scotland/Wales → United Kingdom 等）
    """
    raw = " ".join(t.strip() for t in (text_nodes or []) if t and t.strip())
    raw = re.sub(r"\s+", " ", raw).strip()
    if not raw:
        return ""
    segs = [s.strip() for s in raw.split(",") if s.strip()]
    if len(segs) >= 4:
        segs = segs[1:]
    elif len(segs) == 3 and any(k in segs[0] for k in VENUE_KEYWORDS):
        segs = segs[1:]
    if not segs:
        return ""
    segs[-1] = COUNTRY_ALIASES.get(segs[-1], segs[-1])
    return ",".join(segs)


def split_city_country(address):
    """库内形态 `City,State,Country` → (city, country)，契约见《db-schema》§2.2。

    规则：首段 = 城市、末段 = 国家；**单段 = 国家、城市留空**
    （如源地址只有 `Brazil` 的场次）；空串 → 双空。

    与 UfcMaker 的 `EventpassSpider.parse_address` 同口径（另含 Macao 特例，
    属 ufc.com 时代的数据形态；Sherdog 侧走国家段别名归一，不需要）。
    """
    segs = [s.strip() for s in (address or "").split(",") if s.strip()]
    if not segs:
        return "", ""
    if len(segs) == 1:
        return "", segs[0]
    return segs[0], segs[-1]


def iso_to_unix(value):
    """`2026-09-19T00:00:00+00:00` → `1789776000`（字符串型 Unix 秒；解析失败返回空串）。

    ⚠️ Sherdog 只给**日期**（当天 00:00 UTC，不是真实开赛时刻）——App 端倒计时与
    「到分钟」的日期会偏早，属已登记的源站缺口。
    """
    text = (value or "").strip()
    if not text:
        return ""
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return str(int(dt.timestamp()))


def map_method(raw):
    """Sherdog 结束方式 → 库内 ufc.com 词表（命中现有翻译缓存；未识别原样透传）。"""
    text = re.sub(r"\s+", " ", (raw or "")).strip()
    if not text:
        return ""
    low = text.lower()
    if low.startswith(("ko", "tko")):
        return "TKO - Doctor's Stoppage" if "doctor" in low else "KO/TKO"
    if low.startswith(("submission", "technical submission")):
        return "Submission"
    if low.startswith("decision"):
        if "unanimous" in low:
            return "Decision - Unanimous"
        if "split" in low:
            return "Decision - Split"
        if "majority" in low:
            return "Decision - Majority"
        return text
    if low.startswith("disqualification"):
        return "DQ"
    if low.startswith("no contest"):
        return "No Contest"
    if low.startswith("draw"):
        return "DRAW"
    if low.startswith("could not continue"):
        return "Could Not Continue"
    if low.startswith("overturned"):
        return "Overturned"
    return text


def _weight_alias(weight):
    """`Strawweight` → 女子草量级（UFC 无男子草量级）；`Catchweight 140 lb` → `Catchweight`。"""
    low = weight.lower()
    if low == "strawweight":
        return "Women's Strawweight"
    if low.startswith("catchweight"):
        return "Catchweight"
    return weight


def map_division(weight_class, is_title=False):
    """Sherdog 量级 → 库内 `card_division` 口径（`<Weight> Bout` / `<Weight> Title Bout`）。

    ⚠️ Sherdog 不区分男女（女子蝇量级也只写 Flyweight），唯一可判的是 Strawweight；
    其余女子量级不加前缀（计划 §7 D2）。
    """
    weight = re.sub(r"\s+", " ", (weight_class or "")).strip()
    if not weight:
        return ""
    return _weight_alias(weight) + (" Title Bout" if is_title else " Bout")


def map_player_division(weight_class):
    """选手量级 → 库内 `player.division` 口径（如 `Flyweight Division`）。"""
    weight = re.sub(r"\s+", " ", (weight_class or "")).strip()
    if not weight:
        return ""
    return _weight_alias(weight) + " Division"


def parse_height_inches(text):
    """`5'5"` → `65.00`（库内口径：英寸、两位小数）。"""
    m = re.search(r"(\d+)\s*'\s*(\d+)", text or "")
    if not m:
        return ""
    return "{:.2f}".format(int(m.group(1)) * 12 + int(m.group(2)))


def parse_weight_pounds(text):
    """`125 lbs` → `125.00`（库内口径：磅、两位小数）。"""
    m = re.search(r"(\d+(?:\.\d+)?)\s*lbs?", text or "", re.I)
    if not m:
        return ""
    return "{:.2f}".format(float(m.group(1)))


def parse_birthdate(text):
    """`Oct 10, 2001` → `2001-10-10`；解析不出返回空串。"""
    m = re.search(r"([A-Za-z]{3})[a-z]*\.?\s+(\d{1,2}),\s*(\d{4})", text or "")
    if not m:
        return ""
    month = MONTHS.get(m.group(1).lower())
    if not month:
        return ""
    return "{}-{:02d}-{:02d}".format(m.group(3), month, int(m.group(2)))


def short_date(text):
    """`Sep / 19 / 2026` → `9/19/26`（与库内 history 行首日期写法一致）。"""
    m = re.search(r"([A-Za-z]{3})\s*/\s*(\d{1,2})\s*/\s*(\d{4})", text or "")
    if not m:
        return ""
    month = MONTHS.get(m.group(1).lower())
    if not month:
        return ""
    return "{}/{}/{}".format(month, int(m.group(2)), m.group(3)[2:])


def method_phrase(method):
    """结束方式 → 叙述短语（`Decision (Unanimous)` → `unanimous decision`）。"""
    low = (method or "").lower()
    if "unanimous" in low:
        return "unanimous decision"
    if "split" in low:
        return "split decision"
    if "majority" in low:
        return "majority decision"
    if low.startswith("decision"):
        return "decision"
    if low.startswith(("ko", "tko")):
        return "KO/TKO"
    if "submission" in low:
        return "submission"
    if low.startswith("disqualification"):
        return "disqualification"
    return ""


def build_history_line(name, result, opponent, method, date_text, end_round, end_time):
    """合成一行战绩叙述（供翻译链路出 `history_cn`）。

    例：`(9/19/26) Van defeated Alexandre Pantoja by unanimous decision`
        `(7/13/24) Van lost to Charles Johnson by KO/TKO in round 3 at 0:20`
    """
    date = short_date(date_text)
    if not date or not opponent or not result:
        return ""
    if result == "Win":
        action = "defeated {}".format(opponent)
    elif result == "Loss":
        action = "lost to {}".format(opponent)
    elif result == "Draw":
        action = "drew with {}".format(opponent)
    else:  # NC
        action = "fought {} to a no contest".format(opponent)
    line = "({}) {} {}".format(date, name, action)
    phrase = method_phrase(method)
    if phrase and result in ("Win", "Loss"):
        line += " by {}".format(phrase)
    elif phrase and result == "Draw" and "decision" in phrase:
        line += " by {}".format(phrase)
    if result in ("Win", "Loss") and end_round and end_time:
        line += " in round {} at {}".format(end_round, end_time)
    return line


# ---------- 选手页（两个爬虫共用） ----------

def parse_record(response):
    """Wins/Losses/Draws 三数 → `18-2-0 (W-L-D)`（与库内 ufc.com 口径一致）。"""
    counts = {}
    blocks = response.xpath(
        '//div[{}]//div[{}]'.format(cls_token("winsloses-holder"), cls_token("winloses"))
    )
    for block in blocks:
        nodes = block.xpath("./span/text()").getall()
        if len(nodes) < 2:
            continue
        label = nodes[0].strip().lower()
        num = nodes[1].strip()
        if label.startswith("win"):
            counts["w"] = num
        elif label.startswith("loss"):
            counts["l"] = num
        elif label.startswith("draw"):
            counts["d"] = num
    if not counts:
        return ""
    return "{}-{}-{} (W-L-D)".format(
        counts.get("w", "0"), counts.get("l", "0"), counts.get("d", "0")
    )


def parse_wins_stats(response):
    """三个胜场 meter（KO/TKO、SUBMISSIONS、DECISIONS）→ wins_stats JSON 结构。"""
    stats = []
    titles = response.xpath(
        '//div[{}]/div[{}]//div[{}]'.format(
            cls_token("winsloses-holder"), cls_token("wins"), cls_token("meter-title")
        )
    )
    for title in titles:
        label = re.sub(r"\s+", "", "".join(title.xpath(".//text()").getall())).lower()
        way = WINS_STATS_WAY.get(label)
        if not way:
            continue
        count = title.xpath(
            'following-sibling::div[1]//div[{}]/text()'.format(cls_token("pl"))
        ).get(default="").strip()
        stats.append({"way": way, "times": count or "0"})
    return stats


def parse_history(response, name, logger):
    """FIGHT HISTORY - PRO 表 → 与库内风格一致的英文叙述行（供翻译链路出 history_cn）。"""
    lines = []
    rows = response.xpath(
        '//div[{}]//table//tr[not(@class)]'.format(cls_token("fight_history"))
    )
    for row in rows:
        result = result_token(row)
        opponent = " ".join(row.xpath("./td[2]//a//text()").getall()).strip()
        if not opponent:
            opponent = " ".join(row.xpath("./td[2]//text()").getall()).strip()
        method = row.xpath("./td[4]//b//text()").get(default="").strip()
        date_text = row.xpath('./td[3]//span[{}]//text()'.format(cls_token("sub_line"))).get(default="")
        end_round = " ".join(row.xpath("./td[last()-1]//text()").getall()).strip()
        end_time = " ".join(row.xpath("./td[last()]//text()").getall()).strip()
        line = build_history_line(name, result, opponent, method, date_text, end_round, end_time)
        if line:
            lines.append(line)
    if not lines:
        logger.warning(f"[fighter] 未取到历史战绩: {response.url}")
    return lines


def build_player_item(response, page, logger):
    """Sherdog 选手页 → `UfcPlayerItem`（eventpass / upcoming 共用的唯一实现）。

    `page` 必须由调用方传**请求 URL**（页面给的 href）：万一发生 301，仍与
    `pass_card.red/blue_page` 及 coming JSON 的 red/blue 引用逐字一致。
    缺口列（reach / leg_reach / style / status / debut / cover）写空串，
    导出管道要求键存在；App 端各自降级。
    """
    player = UfcPlayerItem()
    if response.url != page:
        logger.warning(f"[fighter] 页面发生重定向（仍按请求 URL 记账）: {page} -> {response.url}")
    player["page"] = page
    player["name"] = " ".join(
        response.xpath('//h1[@itemprop="name"]//span[@class="fn"]//text()').getall()
    ).strip()
    if not player["name"]:
        logger.warning(f"[fighter] 未取到姓名: {response.url}")
    nick = " ".join(response.xpath('//span[@class="nickname"]//text()').getall()).strip()
    # 剥离首尾各类引号（英文/中文/单/双）
    player["nick_name"] = re.sub(r'^[\'"“”‘’]+|[\'"“”‘’]+$', '', nick).strip()

    # 头像：200×300 竖图；兜底 og:image（300×300 方图）；都取不到留空
    portrait = response.xpath('//img[contains(@class,"profile-image-mobile")]/@src').get(default="")
    if not portrait:
        portrait = response.xpath('//meta[@property="og:image"]/@content').get(default="")
        if portrait:
            logger.warning(f"[fighter] 未取到竖版头像，降级 og:image: {response.url}")
    player["avatar"] = abs_url(portrait)
    # Sherdog 无全身照：留空 → App 头像位用「加载失败默认图」
    player["cover"] = ""

    # 国籍 / 出生地（locality 形如 `Hakha, Chin`）
    country = " ".join(
        response.xpath('//strong[@itemprop="nationality"]//text()').getall()
    ).strip()
    locality = " ".join(
        response.xpath('//span[@itemprop="addressLocality"]//text()').getall()
    ).strip()
    city = locality.split(",")[0].strip() if locality else ""
    player["country"] = country
    player["city"] = city
    player["home_town"] = ", ".join(x for x in (city, country) if x)

    # bio 表：生日 / 身高 / 体重
    player["birthdate"] = parse_birthdate(
        response.xpath('//span[@itemprop="birthDate"]/text()').get(default="")
    )
    player["height"] = parse_height_inches(
        response.xpath('//b[@itemprop="height"]/text()').get(default="")
    )
    player["weight"] = parse_weight_pounds(
        response.xpath('//b[@itemprop="weight"]/text()').get(default="")
    )

    # 团队（ASSOCIATION）/ 量级（CLASS）
    player["team"] = " ".join(
        response.xpath('//a[@class="association"]//span[@itemprop="name"]//text()').getall()
    ).strip()
    player["division"] = map_player_division(
        response.xpath('//a[contains(@href,"weightclass=")]//text()').get(default="")
    )

    player["record"] = parse_record(response)
    player["wins_stats"] = parse_wins_stats(response)
    player["history"] = parse_history(response, player["name"], logger)

    # 缺口列：写空串（App 侧各自降级）
    player["reach"] = ""
    player["leg_reach"] = ""
    player["style"] = ""
    player["status"] = ""
    player["debut"] = ""
    logger.info(
        f"抓取完成选手: {player['name']}，战绩 {player['record']}，"
        f"历史 {len(player['history'])} 条"
    )
    return player