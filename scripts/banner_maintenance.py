# -*- coding: utf-8 -*-
"""历史赛事封面回填脚本（存量数据处理入口）。

爬虫侧已自动执行：eventpass 收尾时 JsonWriterPipeline.close_spider 会先拼接封面、
再导出 ufc_pass_data.json（见 ufcjson/banner.py、plans/eventpass-sherdog.md §7 D3）。
本脚本用于：
1. **存量回填** —— 已经爬好的历史赛事补齐封面（只补缺失，幂等，可反复跑）；
2. **改版式后重拼** —— 加 `--force` 覆盖全部已生成的封面。

用法（在 DogMaker 目录执行）：
    python -m scripts.banner_maintenance            # 只补缺失
    python -m scripts.banner_maintenance --force    # 全量重拼（改设计后）
"""

import argparse
import os
import sys

from ufcjson.banner import DEFAULT_DB, DEFAULT_IMAGES, compose_event_banners


def main():
    parser = argparse.ArgumentParser(description='历史赛事封面回填（头条双方头像 → banner_local）')
    parser.add_argument('--db', default=DEFAULT_DB, help=f'数据库路径（默认 {DEFAULT_DB}）')
    parser.add_argument('--images', default=DEFAULT_IMAGES,
                        help=f'图片目录（默认 {DEFAULT_IMAGES}）')
    parser.add_argument('--force', action='store_true',
                        help='已存在的封面也重拼（改版式后回填用）')
    args = parser.parse_args()

    if not os.path.isfile(args.db):
        print(f'[ERROR] 数据库不存在: {args.db}', file=sys.stderr)
        return 1
    compose_event_banners(args.db, args.images, force=args.force)
    return 0


if __name__ == '__main__':
    sys.exit(main())