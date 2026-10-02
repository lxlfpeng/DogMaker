import json
import sqlite3

from ufcjson.items import (
    UfcPassItem,
    UfcPassCardItem,
    UfcPlayerItem,
)
from ufcjson.spiders.eventpass import EventpassSpider


def dump_cn_field(value):
    """中文 JSON 列（history_cn / wins_stats_cn）的落库口径。

    没有译文时写空串，**不要**写 '[]'。translator._collect_pending 只把
    「NULL 或空串」当作待翻译；写成 '[]' 会被当成「已翻译」而永远跳过。
    空列表 / None / '' 一律落成 ''。"""
    if not value:
        return ''
    return str(json.dumps(value))


# 用于写入sqlite3数据库的管道
class SqliteDbPipeline(object):
    # 构造方法（初始化对象时执行的方法）
    def __init__(self, crawler):
        self.crawler = crawler

    @classmethod
    def from_crawler(cls, crawler):
        return cls(crawler)

    def open_spider(self):
        spider = self.crawler.spider
        # 1. 连接到数据库（如果没有数据库文件，会自动创建）
        self.conn = sqlite3.connect('output/db/ufc.db')
        self.conn.row_factory = sqlite3.Row   # 让 fetchone 返回可按列名访问的 Row（process_item 里 row['history'] 等依赖它）
        # 2. 创建游标对象（用于执行SQL语句）
        self.cursor = self.conn.cursor()
        if isinstance(spider, EventpassSpider):
            # ⚠️ 必须与契约一致：pass_event = 13 列（Resources/contract/db-schema.md §2.2）。
            # 老版本 DDL 多建过 city / country / city_cn / country_cn 四列（声明未用），
            # 实测导出库根本没有这四列、端上也按 13 列校验——重建库时按 13 列建，
            # 不要把这四列加回来（地点只能靠 address 拆段）。
            self.cursor.execute('''
            CREATE TABLE IF NOT EXISTS pass_event (
             id INTEGER PRIMARY KEY AUTOINCREMENT,  -- 主键
             name TEXT,                             -- 名称
             title TEXT,                            -- 头条主赛
             banner TEXT,                           -- 横幅
             address TEXT,                          -- 地点
             address_cn TEXT,                       -- 地点(cn)
             page TEXT UNIQUE,                      -- 主页
             main_time TEXT,                        -- 主卡时间
             prelims_time TEXT,                     -- 副卡时间
             data_early_time TEXT,                  -- 早卡时间
             banner_local TEXT,                     -- 横幅(本地)
             name_cn TEXT,                          -- 名称(中文)
             title_cn TEXT                          -- 头条主赛(中文)
            )
            ''')
            self.cursor.execute('''
            CREATE TABLE IF NOT EXISTS pass_card (
             id INTEGER PRIMARY KEY AUTOINCREMENT,  -- 主键
             fight_page TEXT,                       -- 主页
             blue_page TEXT,                        -- 蓝方主页
             red_page TEXT,                         -- 红方主页
             blue_result TEXT,                      -- 蓝方结果
             red_result TEXT,                       -- 红方结果
             blue_odds TEXT,                        -- 蓝方odds
             red_odds TEXT,                         -- 红方odds
             end_method TEXT,                       -- 结束方式
             end_round TEXT,                        -- 结束回合
             end_time TEXT,                         -- 结束时间
             card_type TEXT,                        -- 类型(主赛复赛)
             card_division  TEXT,                   -- 级别
             end_method_cn TEXT,                    -- 结束方式(中文)
             card_division_cn TEXT                  -- 级别(中文)
            )
            ''')
        # player 表：ufc.com 版 athlete 爬虫已移除（2026-09-30），现在由 EventpassSpider
        # （代表所有会产出 UfcPlayerItem 的 Sherdog 爬虫：eventpass / upcoming / ranking）建表
        if isinstance(spider, EventpassSpider):
            self.cursor.execute('''
                CREATE TABLE IF NOT EXISTS player (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,     -- 用户名
                    name_cn TEXT,           -- 用户名(中文)
                    nick_name TEXT,         -- 昵称
                    nick_name_cn TEXT,      -- 昵称(中文)
                    page TEXT UNIQUE,       -- 个人主页
                    division TEXT,          -- 级别
                    avatar TEXT,            -- 头像
                    avatar_local TEXT,      -- 头像(本地)
                    cover TEXT,             -- 封面
                    cover_local TEXT,       -- 封面(本地)
                    record TEXT,            -- 战绩
                    status TEXT,            -- 状态
                    home_town TEXT,         -- 出生地(城市, 国家)
                    team TEXT,              -- 团队
                    style TEXT,             -- 风格
                    height TEXT,            -- 身高
                    weight TEXT,            -- 体重
                    reach TEXT,             -- 臂展
                    leg_reach TEXT,         -- 腿长
                    debut TEXT,             -- 首次亮像
                    history TEXT,           -- 历史
                    wins_stats TEXT,        -- 获胜方式
                    flag TEXT,              -- 国旗
                    history_cn TEXT,        -- 历史(中文)
                    city TEXT,              -- 城市
                    city_cn TEXT,           -- 城市(中文)
                    country TEXT,           -- 国家
                    country_cn TEXT,        -- 国家(中文)
                    division_cn TEXT,       -- 级别(中文)
                    status_cn TEXT,         -- 状态(中文)
                    team_cn TEXT,           -- 团队(中文)
                    style_cn TEXT,          -- 风格(中文)
                    wins_stats_cn TEXT,     -- 获胜方式(中文)
                    birthdate TEXT          -- 出生日期(YYYY-MM-DD 精确 / YYYY 只知年)
                )
            ''')
            # 契约要求的 2 张辅助表（db-schema.md §2.4/§2.5）：Sherdog 数据不产生
            # 归并/探测记录（选手页 URL 带稳定 id），但**表必须存在**（App 端数据库更新
            # 中心会 COUNT(player_url_alias)，缺表即报错）—— 所以在这里建空表。
            # 原来由 ufcjson/normalize.py、athlete_url.py 在 run.py 收尾时创建，
            # 那两个模块已随 ufc.com 时代的 slug 归并机制一并移除（2026-09-30）。
            self.cursor.execute('''
            CREATE TABLE IF NOT EXISTS player_url_alias (
                id       INTEGER PRIMARY KEY AUTOINCREMENT,
                old_page TEXT NOT NULL UNIQUE,  -- 改版前的选手主页
                new_page TEXT NOT NULL          -- 合并后保留的主页
            )
            ''')
            self.cursor.execute('''
            CREATE TABLE IF NOT EXISTS player_url_probe (
                url        TEXT PRIMARY KEY,
                final      TEXT NOT NULL DEFAULT '',
                checked_at INTEGER NOT NULL DEFAULT 0
            )
            ''')

    def process_item(self, item):
        if isinstance(item, UfcPassItem):
            # 一场赛事在表里只应有一行。刷新窗口内的赛事会被重抓（spider 侧刻意放行），
            # 重抓后再次入库时命中已有行则更新、未命中才插入。
            # ⚠️ 之前是纯 INSERT：重抓已存在的赛事会撞 page 的 UNIQUE 约束（IntegrityError），
            # 整条 item 被 Scrapy 丢弃 ⇒ 事件级字段写库即定型、之后永不更新（2026-10-02 修复）。
            # 与 pass_card 同口径：只覆盖新抓到的非空值，避免把已有数据清空。
            row = self.cursor.execute(
                'SELECT id FROM pass_event WHERE page = ? LIMIT 1', (item.get('url', ''),)).fetchone()
            update_fields = ('name', 'name_cn', 'title', 'title_cn', 'banner', 'banner_local',
                             'address', 'address_cn', 'main_time', 'prelims_time', 'data_early_time')
            if row:
                set_clause = ', '.join(f"{f} = CASE WHEN ? <> '' THEN ? ELSE {f} END" for f in update_fields)
                args = []
                for f in update_fields:
                    value = item.get(f, '')
                    args += [value, value]
                args.append(row[0])
                self.cursor.execute(f'UPDATE pass_event SET {set_clause} WHERE id = ?', args)
            else:
                self.cursor.execute('''
                         INSERT INTO pass_event (name,name_cn,title,title_cn,banner,banner_local,address,address_cn,page,main_time,prelims_time,data_early_time)
                               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                       ''', (item.get('name', ''), item.get('name_cn', ''), item.get('title', ''), item.get('title_cn', ''),
                             item.get('banner', ''),
                             item.get('banner_local', ''),
                             item.get('address', ''), item.get('address_cn', ''), item.get('url', ''),
                             item.get('main_time', ''),
                             item.get('prelims_time', ''), item.get('data_early_time', '')
                             ))
            # # 5. 提交更改
            self.conn.commit()
        if isinstance(item, UfcPassCardItem):
            # 一场对局（赛事 + 双方主页）在表里只应有一行。
            # 爬虫的判重只做在 pass_event 上、且发生在列表页解析时，真正写库在详情页抓回之后，
            # 中间隔着一次网络请求 —— 同一赛事被派发两次时两次都会通过检查，整张战卡被写两遍。
            # 这里按 (fight_page, blue_page, red_page) 兜底：命中则更新，未命中才插入。
            fight_key = (item.get('fight_page', ''), item.get('blue_page', ''), item.get('red_page', ''))
            row = self.cursor.execute(
                'SELECT id FROM pass_card WHERE fight_page = ? AND blue_page = ? AND red_page = ? LIMIT 1',
                fight_key).fetchone()
            update_fields = ('blue_result', 'red_result', 'blue_odds', 'red_odds',
                             'end_method', 'end_method_cn', 'end_round', 'end_time',
                             'card_type', 'card_division', 'card_division_cn')
            if row:
                # 已有该对局：只覆盖新抓到的非空值，避免"赛前写入的空结果"把"赛后的结果"覆盖掉
                set_clause = ', '.join(f"{f} = CASE WHEN ? <> '' THEN ? ELSE {f} END" for f in update_fields)
                args = []
                for f in update_fields:
                    value = item.get(f, '')
                    args += [value, value]
                args.append(row[0])
                self.cursor.execute(f'UPDATE pass_card SET {set_clause} WHERE id = ?', args)
            else:
                self.cursor.execute('''
                       INSERT INTO pass_card (fight_page,blue_page,red_page,blue_result,red_result,blue_odds,red_odds,
                       end_method,end_method_cn,end_round,end_time,card_type,card_division,card_division_cn)
                             VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                     ''', (fight_key[0], fight_key[1], fight_key[2],
                           item.get('blue_result', ''), item.get('red_result', ''), item.get('blue_odds', ''),
                           item.get('red_odds', ''), item.get('end_method', ''), item.get('end_method_cn', ''),
                           item.get('end_round', ''),
                           item.get('end_time', '')
                           , item.get('card_type', ''), item.get('card_division', ''), item.get('card_division_cn', '')
                           ))
            # # 5. 提交更改
            self.conn.commit()
        if isinstance(item, UfcPlayerItem):
            history = item.get('history') or []
            wins_stats = item.get('wins_stats') or []

            # 判断选手是否已存在
            row = self.cursor.execute(
                'SELECT * FROM player WHERE page = ?', (item['page'],)).fetchone()

            if row:
                # ══ 已存在：老值打底，只用「本次抓到的可信非空值」覆盖 ══
                # birthdate、译文列（name_cn 等）不在这批字段里 → 永不覆盖
                final = dict(row)
                for col in ('name', 'division', 'avatar', 'avatar_local', 'cover', 'cover_local',
                            'record', 'status', 'home_town', 'city', 'country', 'team', 'style',
                            'height', 'weight', 'reach', 'leg_reach', 'debut', 'nick_name', 'flag'):
                    new = (item.get(col) or '').strip()
                    if new:                              # 本次抓空的字段，保留老值
                        final[col] = new
                # history/wins_stats：页面结构变化时会抓成空列表，
                # '[]' 会把真实战绩清掉 → 空的不覆盖；源真变了译文才作废
                if history:
                    new_history = json.dumps(history)
                    if new_history != (row['history'] or ''):
                        final['history'] = new_history
                        final['history_cn'] = ''         # 战绩变了，译文作废，translator 重译
                if wins_stats:
                    new_wins = json.dumps(wins_stats)
                    if new_wins != (row['wins_stats'] or ''):
                        final['wins_stats'] = new_wins
                        final['wins_stats_cn'] = ''

                self.cursor.execute(
                    '''
                    UPDATE player SET
                        name=?, division=?, avatar=?, avatar_local=?, cover=?, cover_local=?,
                        record=?, status=?, home_town=?, city=?, country=?, team=?, style=?,
                        height=?, weight=?, reach=?, leg_reach=?, debut=?, nick_name=?, flag=?,
                        wins_stats=?, history=?, wins_stats_cn=?, history_cn=?
                    WHERE id=?
                    ''', (final['name'], final['division'], final['avatar'], final['avatar_local'],
                          final['cover'], final['cover_local'], final['record'], final['status'],
                          final['home_town'], final['city'], final['country'], final['team'], final['style'],
                          final['height'], final['weight'], final['reach'], final['leg_reach'],
                          final['debut'], final['nick_name'], final['flag'],
                          final['wins_stats'], final['history'], final['wins_stats_cn'], final['history_cn'],
                          row['id']))
            else:
                # ══ 不存在：全新选手，全量插入 ══
                self.cursor.execute(
                    '''
                    INSERT INTO player (name, page,division,division_cn,avatar,avatar_local,cover,cover_local,record,birthdate,status,status_cn,
                    home_town,city,city_cn,country,country_cn,team,team_cn,style,style_cn,height,weight,reach,leg_reach,debut,nick_name,wins_stats,wins_stats_cn,
                    history,name_cn,flag,nick_name_cn,history_cn)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    ''', (item['name'], item['page'], item['division'], item.get('division_cn', ''), item['avatar'],
                          item.get('avatar_local', ''),
                          item['cover'], item.get('cover_local', ''), item.get('record', ''), item.get('birthdate', ''),
                          item.get('status', ''), item.get('status_cn', ''), item.get('home_town', ''),
                          item.get('city', ''), item.get('city_cn', ''), item.get('country', ''), item.get('country_cn', ''),
                          item.get('team', ''), item.get('team_cn', ''), item.get('style', ''), item.get('style_cn', ''),
                          item.get('height', ''), item.get('weight', ''),
                          item.get('reach', ''), item.get('leg_reach', '')
                          , item.get('debut', ''), item.get('nick_name', ''), str(json.dumps(item.get('wins_stats'))),
                          dump_cn_field(item.get('wins_stats_cn')),
                          str(json.dumps(history)), item.get('name_cn', ''), item.get('flag', ''),
                          item.get('nick_name_cn', ''),
                          dump_cn_field(item.get('history_cn')))
                )
            # # 5. 提交更改
            self.conn.commit()
        return item

    def close_spider(self):
        # 7. 关闭连接
        self.conn.close()
