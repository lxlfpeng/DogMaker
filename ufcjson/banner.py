# -*- coding: utf-8 -*-
"""历史赛事封面拼接（Sherdog 数据源）：头条主赛双方头像 → 封面带底图。

背景与改判
----------
Sherdog 事件页唯一的事件图是 `image_vs/<id>`（200×100 的头像拼图 + VS 徽标 + 站点水印），
尺寸过小且非海报，不足以做封面 —— 见 plans/eventpass-sherdog.md §7 D3。
2026-10-02 用户改判（原「留空」作废）：用**头条主赛双方**已下载的选手头像（200×300 竖版）
由 Pillow 拼成封面底图；封面带上的渐变与三行文字仍由 App 端叠加
（《历史赛事页面》§7.3：底图 + 底部渐变 + 赛事名 / 日期场馆 / 战果摘要）。

构图（2 倍图，与 App 封面带 353×216 同比例 → centerCrop 不裁切）
--------------------------------------------------------------
· 画布 706×432，深色底 #101014（App 深色主题口径）
· 左 = 蓝方、右 = 红方（与列表卡「蓝方在左、红方在右」口径一致），各铺满半区、**双图紧贴无缝**
· 铺满用 cover 裁切、顶部锚点 0.15（多年代样本实测不切头；200×300 竖图裁掉的是下半身）
· VS 圆徽压在**中缝**上（盖住两图接缝，用户 2026-10-02 定的版式）

产物与调用
----------
· 文件：output/images/full/<sha1("banner|" + fight_page)>.webp（确定性命名，重拼覆盖同一文件）
· 落库：pass_event.banner_local；banner 保持空串（App《历史赛事页面》§9 禁用 banner 原图）
· 头条双方头像缺失的赛事保持空串 → App 走「深色纯色底 + 渐变」降级（§8.1）
· 自动：JsonWriterPipeline.close_spider（eventpass 收尾、生成 ufc_pass_data.json 之前）
· 手动回填：python -m scripts.banner_maintenance [--force]（存量数据 / 改版式后重拼）
"""

import hashlib
import os
import sqlite3
import time

from PIL import Image, ImageDraw, ImageFont, ImageOps

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_DB = os.path.join(BASE_DIR, 'output', 'db', 'ufc.db')
DEFAULT_IMAGES = os.path.join(BASE_DIR, 'output', 'images')

# App 历史详情页封面带 353×216dp 的 2 倍图（centerCrop，同比例不裁切）
BANNER_W, BANNER_H = 706, 432
_BG = (16, 16, 20)        # #101014
# 半区铺满的裁切锚点：0 = 贴顶（保头），0.15 让头部略留余量又不浪费上半区
_CROP_ANCHOR = 0.15
_VS_RADIUS = 38
_FONT_SIZE = 32

_FONT_CANDIDATES = (
    '/System/Library/Fonts/Supplemental/Arial Bold.ttf',       # macOS
    '/System/Library/Fonts/Helvetica.ttc',                     # macOS 兜底
    '/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf',    # Linux（CI）
    '/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf',
    'C:/Windows/Fonts/arialbd.ttf',                            # Windows
)


def _log(logger, message):
    if logger is not None:
        logger.info(message)
    else:
        print(message)


def _font(size):
    for path in _FONT_CANDIDATES:
        if os.path.exists(path):
            try:
                return ImageFont.truetype(path, size)
            except OSError:
                continue
    # Pillow ≥10.1 的内置可缩放字体（Aileron），保证徽标上仍有正常大小的 VS
    return ImageFont.load_default(size=size)


def _fill(im):
    """铺满半区（cover 裁切；锚点偏上保头，200×300 竖图裁掉的是下半身）"""
    return ImageOps.fit(im, (BANNER_W // 2, BANNER_H),
                        Image.LANCZOS, centering=(0.5, _CROP_ANCHOR))


def compose_banner(left_path, right_path, out_path):
    """左（蓝方）/ 右（红方）选手头像 → 封面底图 WebP（双图紧贴、VS 徽标压中缝）"""
    canvas = Image.new('RGB', (BANNER_W, BANNER_H), _BG)
    half = BANNER_W // 2
    canvas.paste(_fill(Image.open(left_path).convert('RGB')), (0, 0))
    canvas.paste(_fill(Image.open(right_path).convert('RGB')), (half, 0))

    draw = ImageDraw.Draw(canvas)
    cx, cy = half, BANNER_H // 2
    draw.ellipse((cx - _VS_RADIUS, cy - _VS_RADIUS, cx + _VS_RADIUS, cy + _VS_RADIUS),
                 fill=(28, 28, 34), outline=(90, 90, 100), width=2)
    font = _font(_FONT_SIZE)
    box = draw.textbbox((0, 0), 'VS', font=font)
    draw.text((cx - (box[2] - box[0]) / 2 - box[0], cy - (box[3] - box[1]) / 2 - box[1]),
              'VS', font=font, fill=(235, 235, 240))

    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    canvas.save(out_path, 'WEBP', quality=85)


def compose_event_banners(db_path=DEFAULT_DB, images_dir=DEFAULT_IMAGES, logger=None, force=False):
    """给缺少封面的历史赛事拼接 banner（skip-if-exists，幂等）。

    跳过：banner_local 非空且文件存在（force 时重拼覆盖）
    缓拼：头条主赛不存在，或双方 avatar_local 为空 / 文件缺失 —— 保持空串走 App 深色底降级

    返回 {'composed', 'skipped', 'deferred', 'failed'} 统计。
    """
    started = time.time()
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    stats = {'composed': 0, 'skipped': 0, 'deferred': 0, 'failed': 0}
    try:
        events = conn.execute('SELECT page, banner_local FROM pass_event').fetchall()
        for event in events:
            page = event['page']
            local = (event['banner_local'] or '').strip()
            if not force and local and os.path.isfile(os.path.join(images_dir, local)):
                stats['skipped'] += 1
                continue

            # 头条主赛 = 卡序第 1 张（eventpass 写入顺序：头条主赛先，App 卡序依赖同序）
            hero = conn.execute(
                'SELECT red_page, blue_page FROM pass_card WHERE fight_page = ? '
                'ORDER BY rowid ASC LIMIT 1', (page,)
            ).fetchone()
            # 蓝左红右（与《历史赛事页面》§5.4 列表卡口径一致）
            pair = []
            if hero:
                for side in ('blue_page', 'red_page'):
                    row = conn.execute(
                        'SELECT avatar_local FROM player WHERE page = ?', (hero[side],)
                    ).fetchone()
                    pair.append(((row['avatar_local'] if row else '') or '').strip())
            if not hero or not all(pair) or not all(
                    os.path.isfile(os.path.join(images_dir, p)) for p in pair):
                stats['deferred'] += 1
                continue

            target = f'full/{hashlib.sha1(("banner|" + page).encode("utf-8")).hexdigest()}.webp'
            try:
                compose_banner(os.path.join(images_dir, pair[0]),
                               os.path.join(images_dir, pair[1]),
                               os.path.join(images_dir, target))
            except Exception as e:  # 单场失败不拖垮整批（下次运行会重试）
                stats['failed'] += 1
                _log(logger, f"[banner] 拼接失败 {page}: {e}")
                continue
            conn.execute('UPDATE pass_event SET banner_local = ? WHERE page = ?', (target, page))
            stats['composed'] += 1
        conn.commit()
    finally:
        conn.close()
    _log(logger, "[banner] 封面拼接完成: 合成 {composed} 场, 已存在跳过 {skipped} 场, "
                 "头像缺失缓拼 {deferred} 场, 失败 {failed} 场, 耗时 {secs:.1f}s".format(
                     secs=time.time() - started, **stats))
    return stats